'use strict';
// Pi Remote v0.3.0. Credentials stay in the same origin/localStorage key as v0.1.x.
const $ = id => document.getElementById(id);
const TOKEN_KEY = 'pi_remote_device_token_v1';
const PREF_KEY = 'pi_remote_prefs_v2';
const APPS_PREF_KEY = 'pi_remote_apps_v3';
let token = localStorage.getItem(TOKEN_KEY);
const qrCode = new URLSearchParams(location.hash.slice(1)).get('pair');
if (location.hash.startsWith('#pair=')) history.replaceState(null, '', location.pathname + location.search);
let prefs = {sensitivity: 1.7, naturalScroll: true, haptics: true};
try { prefs = {...prefs, ...JSON.parse(localStorage.getItem(PREF_KEY) || '{}')}; } catch (_) { /* old preferences are optional */ }
prefs.sensitivity = Math.min(3.5, Math.max(0.5, Number(prefs.sensitivity) || 1.7));
let ws = null, connectionState = 'offline', connecting = false;
let backoff = 900, retryTimer = null, lastPong = 0, generation = 0;
let dragging = false, autoDragging = false, heldArrow = null;
const contacts = new Map();
let gesture = 'none', twoFingerMoved = false, twoFingerCancelled = false, twoFingerStart = 0, twoFingerDistance = 0;
let scrollRest = 0, scrollPointer = null, lastTap = null, dragTimer = null;
let pendingDx = 0, pendingDy = 0, lastMotion = 0;
let installPrompt = null;
let discoveredApps = [];
let appPrefs = {favorites:['moonlight'], hidden:[], order:[]};
try {
  const previous = JSON.parse(localStorage.getItem(APPS_PREF_KEY) || '{}');
  if (Array.isArray(previous.favorites)) appPrefs.favorites=previous.favorites.filter(x=>typeof x==='string');
  if (Array.isArray(previous.hidden)) appPrefs.hidden=previous.hidden.filter(x=>typeof x==='string');
  if (Array.isArray(previous.order)) appPrefs.order=previous.order.filter(x=>typeof x==='string');
} catch (_) { /* preferences optional */ }
const iconUrls = new Map();
const pendingIcons = new Set();
let powerEnabled = false;
let moonlightAutoTab = false, moonlightSeenRunning = false;
function saveAppPrefs() {
  localStorage.setItem(APPS_PREF_KEY, JSON.stringify(appPrefs));
  drawAppSections();
}
function changeFavourite(id) {
  appPrefs.favorites = appPrefs.favorites.includes(id) ? appPrefs.favorites.filter(x=>x!==id) : [...appPrefs.favorites,id];
  if (!appPrefs.order.includes(id)) appPrefs.order.push(id);
  saveAppPrefs();
}
function toggleHidden(id) {
  appPrefs.hidden = appPrefs.hidden.includes(id) ? appPrefs.hidden.filter(x=>x!==id) : [...appPrefs.hidden,id];
  saveAppPrefs();
}
function moveFavourite(id,direction) {
  const ordered=discoveredApps.map(x=>x.id).filter(x=>appPrefs.favorites.includes(x));
  ordered.sort((a,b)=>{
    const ia=appPrefs.order.indexOf(a),ib=appPrefs.order.indexOf(b);
    return (ia<0?999:ia)-(ib<0?999:ib);
  });
  const at=ordered.indexOf(id), to=at+direction;
  if(at<0 || to<0 || to>=ordered.length) return;
  [ordered[at],ordered[to]]=[ordered[to],ordered[at]];
  appPrefs.order=ordered;
  saveAppPrefs();
}

function status(text, mode='offline') {
  $('status').textContent = text;
  document.querySelector('.connection').className = 'connection ' + mode;
}
function showPair(message='') {
  $('pairPanel').classList.remove('hidden');
  $('controlPanel').classList.add('hidden');
  $('pairMessage').textContent = message;
}
function showRemote() {
  $('pairPanel').classList.add('hidden');
  $('controlPanel').classList.remove('hidden');
}
function connected() { return connectionState === 'online' && ws?.readyState === WebSocket.OPEN; }
function send(message) {
  // No input commands are buffered or replayed after a reconnect.
  if (!connected()) return false;
  try { ws.send(JSON.stringify(message)); return true; } catch (_) { return false; }
}
function feedback(message, error=false) {
  $('keyboardMessage').textContent = message;
  $('keyboardMessage').classList.toggle('error', error);
}
function appFeedback(message, error=false) {
  $('appsMessage').textContent = message;
  $('appsMessage').classList.toggle('error', error);
}
const APP_ICONS = {moonlight:'🌙', chromium:'🌐', firefox:'🦊', vlc:'▶️', files:'📁'};
function appIcon(app) {
  const icon = document.createElement('span');
  icon.className = 'app-icon'; icon.textContent = APP_ICONS[app.id] || '▣';
  if (app.hasIcon && token && typeof URL.createObjectURL === 'function') {
    if (iconUrls.has(app.id)) {
      const img = document.createElement('img');img.className='app-icon';img.alt='';img.src=iconUrls.get(app.id);
      return img;
    }
    // Authentication stays in the POST body; never leak the device token in a URL.
    if (pendingIcons.has(app.id)) return icon;
    pendingIcons.add(app.id);
    fetch('/api/app-icon',{method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token,id:app.id}),cache:'no-store'})
      .then(async r=>{if(!r.ok) return;if (!r.blob) return;const blob=await r.blob();
        if(blob.type!=='image/png'||blob.size>512000)return;
        iconUrls.set(app.id,URL.createObjectURL(blob));drawAppSections();})
      .catch(()=>{}).finally(()=>pendingIcons.delete(app.id));
  }
  return icon;
}
function requestAppLaunch(app) {
  if (send({type:'app-launch',id:app.id})) {appFeedback('Launching '+app.name+'…');buzz();}
  else appFeedback('Connect to the Pi first.',true);
}
function appCard(app,home=false) {
  const card=document.createElement('div');card.className='app-card'+(!app.available?' unavailable':'');
  const top=document.createElement('div');top.className='app-top';top.appendChild(appIcon(app));
  const details=document.createElement('div');details.style.minWidth='0';
  const name=document.createElement('div');name.className='app-name';name.textContent=app.name;
  const desc=document.createElement('div');desc.className='app-description';
  desc.textContent=app.running?'Running on Pi':(app.available?app.description||'Installed on Pi':'Not installed');
  details.append(name,desc);top.appendChild(details);card.appendChild(top);
  const actions=document.createElement('div');actions.className='app-actions';
  const launch=document.createElement('button');launch.className='launch';launch.textContent=app.running && app.id==='moonlight'?'Controls':'Open';
  launch.disabled=!app.available;launch.addEventListener('click',()=>requestAppLaunch(app));actions.appendChild(launch);
  const fav=document.createElement('button');fav.title='Pin or unpin favourite';
  fav.setAttribute('aria-label',appPrefs.favorites.includes(app.id)?'Unpin '+app.name:'Pin '+app.name);
  fav.textContent=appPrefs.favorites.includes(app.id)?'★':'☆';
  fav.addEventListener('click',()=>changeFavourite(app.id));actions.appendChild(fav);
  if(!home){
    const hide=document.createElement('button');hide.className='hide-action';hide.textContent=appPrefs.hidden.includes(app.id)?'Show':'Hide';
    hide.addEventListener('click',()=>toggleHidden(app.id));actions.appendChild(hide);
    if(appPrefs.favorites.includes(app.id)) for(const [arrow,dir] of [['↑',-1],['↓',1]]){
      const btn=document.createElement('button');btn.textContent=arrow;btn.className='move-action';
      btn.setAttribute('aria-label','Move '+app.name+(dir<0?' up':' down'));
      btn.addEventListener('click',()=>moveFavourite(app.id,dir));actions.appendChild(btn);
    }
  }
  card.appendChild(actions);
  return card;
}
function drawAppSections() {
  const search=($('appsSearch').value||'').trim().toLowerCase();
  const showHidden=$('showHiddenApps').checked;
  const available=discoveredApps.filter(app=>app&&typeof app.id==='string'&&typeof app.name==='string');
  const index=id=>{const i=appPrefs.order.indexOf(id);return i<0?9999:i};
  const list=available.filter(app=>(showHidden||!appPrefs.hidden.includes(app.id)) &&
    (app.name.toLowerCase().includes(search)||(app.description||'').toLowerCase().includes(search)));
  list.sort((a,b)=>index(a.id)-index(b.id) || (a.id==='moonlight'?-1:b.id==='moonlight'?1:a.name.localeCompare(b.name)));
  const grid=$('appsList');grid.replaceChildren();
  if (!list.length) {const t=document.createElement('p');t.className='subtle';t.textContent='No matching apps. Use Refresh applications to scan the Pi.';grid.appendChild(t);}
  for (const app of list) grid.appendChild(appCard(app));
  const fav=$('homeFavorites');fav.replaceChildren();
  const favApps=available.filter(app=>appPrefs.favorites.includes(app.id)&&!appPrefs.hidden.includes(app.id)).sort((a,b)=>index(a.id)-index(b.id));
  if (!favApps.length){const t=document.createElement('p');t.className='subtle';t.textContent='Pin apps with ☆ from the Apps tab.';fav.appendChild(t);}
  for(const app of favApps.slice(0,10))fav.appendChild(appCard(app,true));
}
function renderApps(apps) {
  if (!Array.isArray(apps)) return;
  discoveredApps=apps.filter(x=>x && typeof x.id==='string' && typeof x.name==='string').slice(0,140);
  drawAppSections();
}
function refreshApps() {
  if (!send({type:'apps-list'})) appFeedback('Connect to the Pi to load applications.',true);
}
$('appsSearch').addEventListener('input',drawAppSections);
$('showHiddenApps').addEventListener('change',drawAppSections);
$('refreshApps').addEventListener('click',refreshApps);
function switchTab(name){ const tab=document.querySelector('[data-tab="'+name+'"]');if(tab)tab.click(); }
function displayUptime(seconds){
  if (typeof seconds!=='number') return '—';
  const h=Math.floor(seconds/3600), d=Math.floor(h/24);
  return d ? d+'d '+(h%24)+'h' : h+'h '+Math.floor(seconds%3600/60)+'m';
}
function updateSystem(data) {
  if(!data || typeof data!=='object')return;
  $('cpuTemp').textContent=typeof data.cpuTemp==='number'?data.cpuTemp+'°C':'—';
  $('cpuUse').textContent=typeof data.cpuPercent==='number'?data.cpuPercent+'%':'—';
  $('memUse').textContent=typeof data.memoryPercent==='number'?data.memoryPercent+'%':'—';
  $('uptime').textContent=displayUptime(data.uptime);
  powerEnabled=!!data.powerEnabled;
  $('restartPi').disabled=!powerEnabled;
  $('shutdownPi').disabled=!powerEnabled;
  $('powerHelp').textContent=powerEnabled?'Restart and shutdown require a confirmation. The remote will disconnect until the Pi comes back online.':
    'Power controls disabled. See Settings to enable them on the Pi.';
  $('homeStatus').textContent=data.moonlightRunning?'Moonlight is running on the Pi.':'Your Pi is online and ready.';
  if (moonlightAutoTab) {
    if (data.moonlightRunning) moonlightSeenRunning = true;
    else if (moonlightSeenRunning) {
      moonlightAutoTab = false; moonlightSeenRunning = false;
      if (!$('tab-moonlight').classList.contains('hidden')) switchTab('mouse');
    }
  }
}
function refreshSystem(){send({type:'system-status'});}
function systemFeedback(s,error=false){$('systemMessage').textContent=s;$('systemMessage').classList.toggle('error',error);}
for(const [id,action] of [['restartPi','reboot'],['shutdownPi','shutdown']]) {
  $(id).disabled=true;
  $(id).addEventListener('click',()=>{
    if(!powerEnabled){systemFeedback('Enable power controls in Settings first.',true);return;}
    if(!send({type:'power-prepare',action}))systemFeedback('Pi is disconnected.',true);
  });
}
$('quickMoonlight').addEventListener('click',()=>requestAppLaunch({id:'moonlight',name:'Moonlight'}));
$('homeMouse').addEventListener('click',()=>switchTab('mouse'));
function buzz() {
  if (prefs.haptics && navigator.vibrate) navigator.vibrate(8);
}
function scheduleRetry() {
  clearTimeout(retryTimer);
  if (!token) return;
  const wait = Math.round(backoff * (0.85 + Math.random() * 0.3));
  retryTimer = setTimeout(connect, wait);
  backoff = Math.min(Math.round(backoff * 1.65), 15000);
  status('Reconnecting…', 'waiting');
}
async function verify() {
  const aborter = new AbortController();
  const timeout = setTimeout(() => aborter.abort(), 5000);
  try {
    return await fetch('/api/verify', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token}), signal:aborter.signal, cache:'no-store'
    });
  } finally { clearTimeout(timeout); }
}
function dropInputState() {
  // Server also releases a held mouse button when the socket disappears.
  clearTimeout(dragTimer); dragTimer = null;
  clearHeldArrow();
  contacts.clear(); gesture = 'none'; scrollPointer = null;
  pendingDx = pendingDy = 0;
  dragging = false; autoDragging = false;
  updateDragUI();
}
function disconnect(retry=true) {
  if (ws) {
    const old = ws; ws = null;
    try { old.close(); } catch (_) { /* socket already dead */ }
  }
  dropInputState();
  connectionState = 'offline';
  if (retry) scheduleRetry();
}
async function connect() {
  clearTimeout(retryTimer);
  if (!token || connecting || (ws && ws.readyState <= WebSocket.OPEN)) return;
  const attempt = ++generation;
  connecting = true;
  connectionState = 'connecting';
  status('Connecting…', 'waiting');
  try {
    const response = await verify();
    if (attempt !== generation) return;
    if (response.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      token = null;
      connectionState = 'offline';
      status('Not paired');
      showPair('This device was revoked or the saved pairing is missing. Pair again.');
      return;
    }
    if (!response.ok) throw new Error('Pi not ready');
    showRemote();
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(protocol + '//' + location.host + '/ws');
    ws = socket;
    const authTimeout = setTimeout(() => {
      if (ws === socket && connectionState !== 'online') socket.close();
    }, 8000);
    socket.onopen = () => socket.send(JSON.stringify({type:'auth',token}));
    socket.onmessage = ev => {
      if (socket !== ws) return;
      let data;
      try { data = JSON.parse(ev.data); } catch (_) { return; }
      if (data.type === 'ready') {
        clearTimeout(authTimeout);
        connectionState = 'online'; backoff = 900; lastPong = Date.now();
        status('Connected', 'online');
        refreshApps();
        refreshSystem();
      } else if (data.type === 'pong') {
        lastPong = Date.now();
      } else if (data.type === 'denied') {
        clearTimeout(retryTimer);
        token = null; localStorage.removeItem(TOKEN_KEY);
        connectionState = 'offline';
        dropInputState();
        status('Not paired');
        showPair('The Pi no longer trusts this phone. Pair it again.');
        socket.close();
      } else if (data.type === 'apps') {
        renderApps(data.apps);
        appFeedback('');
      } else if (data.type === 'app-status') {
        appFeedback(data.message || 'Launch result received', !data.ok);
        if (data.ok && data.id==='moonlight') {moonlightAutoTab=true;moonlightSeenRunning=!!data.alreadyRunning;switchTab('moonlight');refreshApps();refreshSystem();}
      } else if (data.type === 'system-info') {
        updateSystem(data.data);
      } else if (data.type === 'power-status') {
        systemFeedback(data.message||'Power command submitted', !data.ok);
      } else if (data.type === 'power-confirm') {
        const action=data.action;
        if(!['reboot','shutdown'].includes(action)||typeof data.nonce!=='string')return;
        const warning=action==='reboot'?'Restart the Raspberry Pi now?':'Shut down the Raspberry Pi now? You may need to turn it on physically.';
        if(window.confirm?.(warning))send({type:'power-execute',action,nonce:data.nonce});
        else systemFeedback('Power action cancelled.');
      } else if (data.type === 'clipboard-status') {
        feedback(data.message || 'Clipboard operation complete', !data.ok);
      } else if (data.type === 'clipboard-data') {
        $('typeInput').value = data.value || '';
        feedback('Pi clipboard loaded into the text field. Use Copy field to phone, or select and copy manually.');
        selectKeyboard();
      }
    };
    socket.onclose = () => {
      clearTimeout(authTimeout);
      if (socket !== ws) return;
      ws = null;
      dropInputState();
      connectionState = 'offline';
      if (!token) { status('Not paired'); return; }
      scheduleRetry();
    };
    socket.onerror = () => socket.close();
  } catch (_) {
    if (attempt === generation && token) {
      connectionState = 'offline';
      scheduleRetry();
    }
  } finally { if (attempt === generation) connecting = false; }
}
function forceReconnect() {
  clearTimeout(retryTimer);
  ++generation;
  connecting = false;
  disconnect(false);
  backoff = 900;
  if (token) connect();
}
$('pairButton').addEventListener('click', async () => {
  const code = $('pairCode').value.trim();
  if (!/^\d{8}$/.test(code)) { $('pairMessage').textContent = 'Enter the eight-digit code from the Pi.'; return; }
  const button = $('pairButton'); button.disabled = true;
  $('pairMessage').textContent = '';
  try {
    const response = await fetch('/api/pair', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({code,name:$('deviceName').value})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not pair');
    token = data.token;
    localStorage.setItem(TOKEN_KEY, token);
    $('pairCode').value = '';
    forceReconnect();
  } catch(e) { $('pairMessage').textContent = e.message || 'Pi unreachable'; }
  finally { button.disabled = false; }
});

function click(button) { if (send({type:'click',button})) buzz(); }
$('leftClick').addEventListener('click', () => click('left'));
$('rightClick').addEventListener('click', () => click('right'));
function updateDragUI() {
  $('dragButton').textContent = dragging ? 'Drag: ON' : 'Drag: off';
  $('dragButton').classList.toggle('active', dragging);
  $('dragButton').setAttribute('aria-pressed', String(dragging));
}
function setDrag(on, automatic=false) {
  if (!connected()) return;
  if (on !== dragging) {
    send({type:'drag',down:on});
    dragging = on;
  }
  autoDragging = automatic && on;
  updateDragUI();
}
$('dragButton').addEventListener('click', () => { if (connected()) { setDrag(!dragging); buzz(); } });
function flushMove(force=false) {
  const now = performance.now();
  if (!force && now-lastMotion < 12) return;
  const dx = Math.max(-250,Math.min(250,Math.round(pendingDx)));
  const dy = Math.max(-250,Math.min(250,Math.round(pendingDy)));
  pendingDx -= dx; pendingDy -= dy;
  if (dx || dy) send({type:'move',dx,dy});
  lastMotion = now;
}
function scrollByPixels(pixels) {
  scrollRest += pixels * (prefs.naturalScroll ? 1 : -1);
  let steps = Math.trunc(scrollRest / 25);
  if (steps) {
    steps = Math.max(-8, Math.min(8, steps));
    send({type:'scroll',amount:steps});
    scrollRest -= steps*25;
  }
}
const pad = $('pad');
pad.addEventListener('pointerdown', e => {
  if (!connected()) return;
  e.preventDefault();
  pad.setPointerCapture(e.pointerId);
  contacts.set(e.pointerId, {x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY,started:Date.now(),distance:0});
  if (contacts.size === 1) {
    gesture = 'single';
    const tap = lastTap;
    if (tap && Date.now()-tap.t < 350 && Math.hypot(e.clientX-tap.x,e.clientY-tap.y)<42) {
      dragTimer = setTimeout(() => {
        if (contacts.size === 1 && gesture === 'single') { setDrag(true,true); buzz(); }
      }, 175);
    }
  } else if (contacts.size === 2) {
    clearTimeout(dragTimer); dragTimer = null;
    if (autoDragging) setDrag(false);
    gesture = 'two'; twoFingerMoved = false; twoFingerCancelled = false;
    twoFingerStart = Date.now(); twoFingerDistance = 0;
    scrollRest = 0;
    lastTap = null;
  } else { gesture = 'many'; }
});
pad.addEventListener('pointermove', e => {
  const c = contacts.get(e.pointerId);
  if (!c) return;
  e.preventDefault();
  const dx = e.clientX-c.x, dy = e.clientY-c.y;
  c.x=e.clientX; c.y=e.clientY;
  c.distance += Math.hypot(dx,dy);
  if (gesture === 'single' && contacts.size === 1) {
    if (c.distance > 13) { clearTimeout(dragTimer); dragTimer=null; }
    pendingDx += dx * prefs.sensitivity;
    pendingDy += dy * prefs.sensitivity;
    flushMove();
  } else if (gesture === 'two' && contacts.size === 2) {
    // Each finger contributes half the wheel movement; the centroid is smooth.
    const pixels = -dy/2;
    twoFingerDistance += Math.hypot(dx, dy);
    if (twoFingerDistance > 17) twoFingerMoved = true;
    scrollByPixels(pixels);
  }
});
function releasePadPointer(e) {
  const c = contacts.get(e.pointerId);
  if (!c) return;
  const wasTwo = gesture === 'two' || gesture === 'ending-two';
  if (wasTwo && e.type !== 'pointerup') twoFingerCancelled = true;
  const duration = Date.now() - c.started;
  if (gesture === 'single' && contacts.size === 1) {
    flushMove(true);
    if (autoDragging) setDrag(false);
    else if (e.type === 'pointerup' && c.distance < 13 && duration < 330) {
      click('left'); lastTap = {t:Date.now(),x:c.x,y:c.y};
    } else if (c.distance > 13) lastTap = null;
  }
  contacts.delete(e.pointerId);
  if (wasTwo && contacts.size === 0) {
    if (!twoFingerMoved && !twoFingerCancelled && Date.now()-twoFingerStart < 340 && e.type === 'pointerup') click('right');
    gesture='none';
  } else if (wasTwo) gesture='ending-two';
  else if (contacts.size === 0) gesture='none';
  clearTimeout(dragTimer); dragTimer=null;
}
pad.addEventListener('pointerup',releasePadPointer);
pad.addEventListener('pointercancel',releasePadPointer);
pad.addEventListener('lostpointercapture', e => { if (contacts.has(e.pointerId)) releasePadPointer(e); });
const scrollPad = $('scroll');
scrollPad.addEventListener('pointerdown', e => {
  if (!connected()) return;
  e.preventDefault(); scrollPad.setPointerCapture(e.pointerId);
  scrollPointer={id:e.pointerId,y:e.clientY};scrollRest=0;
});
scrollPad.addEventListener('pointermove', e => {
  if (!scrollPointer || scrollPointer.id!==e.pointerId) return;
  e.preventDefault(); scrollByPixels(scrollPointer.y-e.clientY);scrollPointer.y=e.clientY;
});
function releaseScroll(e) { if (scrollPointer?.id===e.pointerId) scrollPointer=null; }
scrollPad.addEventListener('pointerup',releaseScroll);
scrollPad.addEventListener('pointercancel',releaseScroll);
scrollPad.addEventListener('lostpointercapture',releaseScroll);

function selectKeyboard() { document.querySelector('[data-tab="keyboard"]').click(); }
document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('[data-tab]').forEach(b => {
    const active = b === button;
    b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active));
    $('tab-'+b.dataset.tab).classList.toggle('hidden',!active);
  });
  if (button.dataset.tab !== 'moonlight' && moonlightAutoTab && button.dataset.tab !== 'apps') moonlightAutoTab=false;
  if (button.dataset.tab === 'apps') refreshApps();
  if (button.dataset.tab === 'home') refreshSystem();
  clearHeldArrow();
}));
document.querySelectorAll('[data-key]').forEach(button => button.addEventListener('click', () => {
  if (send({type:'key',name:button.dataset.key})) buzz();
}));
document.querySelectorAll('[data-combo]').forEach(button => button.addEventListener('click', () => {
  const parts = button.dataset.combo.split('+');
  const name = parts.pop();
  if (send({type:'combo',name,modifiers:parts})) buzz();
}));
// Moonlight dpad: hold to repeat. Release promptly on cancellation, blur, or disconnect.
function clearHeldArrow() {
  if (!heldArrow) return;
  clearTimeout(heldArrow.timeout);
  clearInterval(heldArrow.interval);
  heldArrow = null;
}
document.querySelectorAll('[data-repeat-key]').forEach(button => {
  const stop = () => clearHeldArrow();
  button.addEventListener('pointerdown', e => {
    e.preventDefault(); if (!connected()) return;
    clearHeldArrow();button.setPointerCapture(e.pointerId);
    const name=button.dataset.repeatKey;
    send({type:'key',name});buzz();
    const held={id:e.pointerId,timeout:null,interval:null};heldArrow=held;
    held.timeout=setTimeout(()=>{
      if (heldArrow!==held) return;
      held.interval=setInterval(()=>send({type:'key',name}),110);
    },350);
  });
  button.addEventListener('pointerup',stop);
  button.addEventListener('pointercancel',stop);
  button.addEventListener('lostpointercapture',stop);
});
$('sendButton').addEventListener('click', () => {
  if (!connected()) { feedback('Connect to the Pi first.',true); return; }
  const text=$('typeInput').value;
  if (!text) return;
  // Existing keyboard emulator supports US ASCII; use clipboard-paste for Unicode.
  for (let i=0;i<text.length;i+=200) send({type:'text',value:text.slice(i,i+200)});
  feedback('Sent as keystrokes (unsupported Unicode characters are skipped).');
});
$('clearText').addEventListener('click', () => { $('typeInput').value='';feedback(''); });
$('typeInput').addEventListener('keydown', e => {
  if (e.key==='Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault();$('sendButton').click(); }
});
$('copyToPi').addEventListener('click', () => {
  const value=$('typeInput').value;
  if (!value) { feedback('Enter some text first.',true); return; }
  if (value.length>4000 || new TextEncoder().encode(value).length>12000) { feedback('Clipboard limit: 4000 characters or 12 KB of UTF-8.',true); return; }
  if (!send({type:'clipboard-set',value,action:'copy'})) feedback('Pi not connected.',true);
});
$('pasteToPi').addEventListener('click', () => {
  const value=$('typeInput').value;
  if (!value) { feedback('Paste or type text into the field first.',true);return; }
  if (value.length>4000 || new TextEncoder().encode(value).length>12000) { feedback('Clipboard limit: 4000 characters or 12 KB of UTF-8.',true);return; }
  if (!send({type:'clipboard-set',value,action:'paste'})) feedback('Pi not connected.',true);
});
$('readPhoneClipboard').addEventListener('click', async () => {
  if (!navigator.clipboard?.readText) {
    feedback('Phone clipboard access needs trusted HTTPS. Long-press the field and choose Paste instead.',true);
    $('typeInput').focus(); return;
  }
  try { $('typeInput').value=(await navigator.clipboard.readText()).slice(0,4000);feedback('Phone clipboard loaded. Tap Paste text to Pi.'); }
  catch (_) { feedback('Clipboard permission denied. Long-press the text field and choose Paste.',true); }
});
$('readPiClipboard').addEventListener('click', () => {
  if (!send({type:'clipboard-get'})) feedback('Pi not connected.',true);
});
$('copyToPhone').addEventListener('click', async () => {
  if (!$('typeInput').value) return;
  if (!navigator.clipboard?.writeText) {
    $('typeInput').select();feedback('Phone clipboard requires trusted HTTPS. The text is selected; use Copy from the selection menu.',true);return;
  }
  try { await navigator.clipboard.writeText($('typeInput').value);feedback('Copied to phone clipboard.'); }
  catch (_) { $('typeInput').select(); feedback('Clipboard permission denied; copy the selected text manually.',true); }
});

$('sensitivity').value = String(prefs.sensitivity);
$('naturalScroll').checked = !!prefs.naturalScroll;
$('haptics').checked = !!prefs.haptics;
function savePrefs() {
  localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
  $('speedValue').textContent = prefs.sensitivity.toFixed(1)+'×';
}
savePrefs();
$('sensitivity').addEventListener('input', e => { prefs.sensitivity=Number(e.target.value);savePrefs(); });
$('naturalScroll').addEventListener('change', e => {prefs.naturalScroll=e.target.checked;savePrefs();});
$('haptics').addEventListener('change', e => {prefs.haptics=e.target.checked;savePrefs();});
$('reconnectButton').addEventListener('click',forceReconnect);
window.addEventListener('online',()=>{ if(token) forceReconnect(); });
window.addEventListener('offline',()=>{ if(token) status('Network changed…','waiting'); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearHeldArrow(); if (autoDragging) setDrag(false); return; }
  if (!token) return;
  if (connected() && Date.now()-lastPong<19000) send({type:'ping'});
  else forceReconnect();
});
window.addEventListener('focus', () => {
  if (token && !document.hidden && (!connected() || Date.now()-lastPong>19000)) forceReconnect();
});
window.addEventListener('pagehide', () => {
  clearHeldArrow();
  if (dragging) send({type:'drag',down:false});
});
setInterval(() => {
  if (connected()) {
    if (Date.now()-lastPong>23000) { forceReconnect();return; }
    send({type:'ping'});
    if (!$('tab-home').classList.contains('hidden')) refreshSystem();
  } else if (token && !connecting && !retryTimer && (!ws || ws.readyState===WebSocket.CLOSED)) scheduleRetry();
},7000);

function updateInstallMessage() {
  const standalone = window.matchMedia?.('(display-mode: standalone)').matches || navigator.standalone;
  $('installHelp').textContent = standalone
    ? 'Running as a standalone home-screen app.'
    : (window.isSecureContext
        ? 'Trusted HTTPS active. Use Install app if shown, or Chrome menu → Add to Home screen.'
        : 'On http://steamlink.local, use Chrome menu → Add to Home screen for a shortcut. A fully installable Android PWA and direct phone clipboard access require trusted HTTPS.');
}
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();installPrompt=e;
  $('installButton').classList.remove('hidden');
});
$('installButton').addEventListener('click', async () => {
  if (!installPrompt) return;
  const p=installPrompt; installPrompt=null;
  $('installButton').classList.add('hidden');
  await p.prompt();
});
window.addEventListener('appinstalled',()=>{ $('installButton').classList.add('hidden');updateInstallMessage(); });
updateInstallMessage();
// Service workers require a trusted secure origin. Never register on insecure LAN HTTP.
if (window.isSecureContext && 'serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
if (token) { showRemote();connect(); }
else {
  status('Not paired');showPair();
  if (qrCode && /^\d{8}$/.test(qrCode)) {
    $('pairCode').value=qrCode;
    $('pairMessage').textContent='QR recognised. Tap Pair device to trust this phone.';
  }
}
