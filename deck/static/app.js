'use strict';
/* LAN Deck client.
   Live state arrives over Server-Sent Events (/api/events); commands go out as POSTs.
   Falls back to polling if the event stream is unavailable. */

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const store = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback } catch { return fallback } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)) } catch { } },
};
function showDialog(d) { if (!d.open) d.showModal(); if (document.activeElement && d.contains(document.activeElement) && !d.querySelector('.pin')) document.activeElement.blur() }
const icon = (name, cls = '') => `<svg class="${cls}"><use href="#i-${name}"/></svg>`;
const fmt = s => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
};

/* ---------- Pair from QR code: /#pin=123456 (the hash never reaches the server) ---------- */
const hashPin = (location.hash.match(/pin=(\d{6})/) || [])[1];
if (hashPin) { try { localStorage.setItem('deck-token', hashPin) } catch { } history.replaceState(null, '', location.pathname) }

/* ---------- State ---------- */
const state = {
  token: localStorage.getItem('deck-token') || '',
  apps: [],
  windows: { list: [], supported: true, reason: '' },
  workspaces: null,
  media: { available: false, players: [], active: '' },
  mediaAt: performance.now(),
  player: '',                       // player chosen by the user
  panel: 'home',
  category: 'all',
  conn: 'connecting',               // connecting | live | polling | offline | unpaired
  prefs: { labels: true, media: true, apps: true, desktops: true, size: 'm', ...store.get('deck-prefs2', {}) },
  hidden: new Set(store.get('deck-hidden2', [])),
  pinned: store.get('deck-pinned', []),         // the Home dock, in the user's order
  dockRemoved: new Set(store.get('deck-dock-removed', [])),
  prevRunning: null,
  arranging: false,
  arrangeSel: '',                            // app ids open at the last update (to spot newly opened apps)
  bannerDismissed: store.get('deck-banner-dismissed', ''),
};
const savePrefs = () => store.set('deck-prefs2', state.prefs);
const saveHidden = () => store.set('deck-hidden2', [...state.hidden]);
const savePinned = () => { store.set('deck-pinned', state.pinned); store.set('deck-dock-removed', [...state.dockRemoved]) };

/* ---------- Toast ---------- */
let toastTimer;
function toast(text) {
  const t = $('#toast');
  t.textContent = text; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
}

/* ---------- API ---------- */
async function api(path, body) {
  const res = await fetch('/api/' + path, {
    method: body ? 'POST' : 'GET',
    headers: { Authorization: 'Bearer ' + state.token, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) { setConn('unpaired'); openPair(); throw Error('Pair this device first') }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Error(data.error || 'Request failed');
  return data;
}

/* ---------- Connection (SSE with polling fallback) ---------- */
let source = null, pollTimer = null, retryTimer = null;

function setConn(mode) {
  state.conn = mode;
  const text = { connecting: 'Connecting', live: 'Live', polling: 'Connected', offline: 'Offline', unpaired: 'Not paired' }[mode];
  $('#status-text').textContent = text;
  for (const dot of [$('#status-dot'), $('#rail-dot')]) {
    dot.classList.toggle('live', mode === 'live' || mode === 'polling');
    dot.classList.toggle('off', mode === 'offline' || mode === 'unpaired');
  }
  $('#rail-status').title = text;
  $('#conn-mode').textContent = mode === 'live' ? 'Live updates' : text;
}

function stopLive() {
  source?.close(); source = null;
  clearInterval(pollTimer); pollTimer = null;
  clearTimeout(retryTimer); retryTimer = null;
}

async function connect() {
  stopLive();
  if (!state.token) { setConn('unpaired'); openPair(); return }
  setConn('connecting');
  try { await api('session') } catch (e) {
    if (state.conn !== 'unpaired') { setConn('offline'); retryTimer = setTimeout(connect, 4000) }
    return;
  }
  loadApps();
  if (!('EventSource' in window)) return startPolling();
  source = new EventSource('/api/events?token=' + encodeURIComponent(state.token));
  source.onopen = () => setConn('live');
  source.addEventListener('media', e => applyMedia(JSON.parse(e.data)));
  source.addEventListener('windows', e => applyWindows(JSON.parse(e.data)));
  source.addEventListener('workspaces', e => applyWorkspaces(JSON.parse(e.data)));
  source.onerror = () => {
    // The browser retries on its own; if it gave up (closed), reconnect ourselves.
    if (source?.readyState === EventSource.CLOSED) { setConn('offline'); retryTimer = setTimeout(connect, 3000) }
    else setConn('connecting');
  };
}

function startPolling() {
  setConn('polling');
  const tick = async () => {
    try {
      const [m, w, ws] = await Promise.all([api('media'), api('windows'), api('workspaces')]);
      applyMedia(m); applyWindows(w); applyWorkspaces(ws); setConn('polling');
    } catch { if (state.conn !== 'unpaired') setConn('offline') }
  };
  tick(); pollTimer = setInterval(tick, 3000);
}

document.addEventListener('visibilitychange', () => {
  // iOS suspends background pages; reconnect cleanly when the deck comes back.
  if (document.hidden) stopLive(); else connect();
});

/* ---------- Data handlers ---------- */
async function loadApps() {
  try { state.apps = (await api('apps')).apps || []; renderApps(); renderHome(); renderMedia(); renderPlayers() }
  catch { }
}
let lastRealWindows = 0;
function applyWindows(d) {
  const list = d.windows || [];
  // A guessed (process-only) list right after a real one is a hiccup: ignore it so tiles don't flicker.
  if (list.some(w => w.process_only) && Date.now() - lastRealWindows < 30000) return;
  if (list.length && !list.some(w => w.process_only)) lastRealWindows = Date.now();
  state.windows = { list, supported: d.supported !== false, reason: d.reason || '' };
  renderHome(); renderApps(); renderSheetWindows();
}
function applyWorkspaces(d) { state.workspaces = d; renderDesktops() }

/* ---------- Helpers ---------- */
const appById = id => state.apps.find(a => a.id === id);
const winKey = w => 'win:' + (w.app_id || w.title);
const runningWindows = () => state.windows.list.filter(w => !state.hidden.has(winKey(w)) && !(w.app_id && state.hidden.has('app:' + w.app_id)));
const runningAppIds = () => new Set(state.windows.list.map(w => w.app_id).filter(Boolean));

function windowName(w) {
  if (w.label) return w.label;
  const siblings = state.windows.list.filter(x => x.app_id && x.app_id === w.app_id).length;
  return siblings > 1 ? (w.title || w.app_name) : (w.app_name || w.title);
}
function tileHTML({ id, kind, img, name, running, minimized, named, slot }) {
  const pic = img ? `<img class="tile-img" src="${esc(img)}" alt="" draggable="false" loading="lazy">` : `<span class="tile-img fallback">${icon(kind === 'window' ? 'apps' : 'apps')}</span>`;
  return `<button class="tile${minimized ? ' minimized' : ''}${slot && slot === state.arrangeSel ? ' selected' : ''}" type="button" data-kind="${kind}" data-id="${esc(id)}"${slot ? ` data-slot="${esc(slot)}"` : ''} title="${esc(name)}" aria-label="${esc(name)}">${pic}<span class="tile-label">${esc(name)}</span>${running ? `<i class="light${minimized ? ' dim' : ''}"></i>` : ''}${named ? icon('edit', 'named') : ''}</button>`;
}
function emptyHTML(svg, title, text) { return `<div class="empty">${icon(svg)}<strong>${esc(title)}</strong>${text ? `<span>${text}</span>` : ''}</div>` }
function setHTML(el, html) { if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html } }

/* ---------- Home ---------- */
const winNum = w => Number(String(w.id).replace(/\D/g, '')) || 0;   // window ids grow as windows open

// Stable order for windows: by the app's place on the dock, then by when each window opened.
function dockSort(wins) {
  const pos = id => { const i = state.pinned.indexOf(id); return i < 0 ? 1e9 : i };
  return [...wins].sort((a, b) => pos(a.app_id) - pos(b.app_id) || winNum(a) - winNum(b));
}

// The Home dock is a list of slots that never reorder by themselves:
//   "<app id>"  - an app; shows its first open window, or the closed app
//   "win:<id>"  - an extra window (2nd terminal, ...), or a window with no app; added at the END
// Opening, minimizing and closing only change the light. Only "Arrange Home" moves slots.
function updateDock(all) {
  const real = all.filter(w => !w.process_only);              // never let guessed entries into the dock
  if (real.length !== all.length) return;
  const open = new Set(real.map(w => w.app_id).filter(Boolean));
  const first = state.prevRunning === null;
  const dock = state.pinned;
  let changed = false;
  const add = key => { if (!dock.includes(key)) { dock.push(key); changed = true } };

  for (const w of [...real].sort((a, b) => winNum(a) - winNum(b))) {
    const id = w.app_id;
    if (id && !dock.includes(id) && (first ? !state.dockRemoved.has(id) : !state.prevRunning.has(id))) {
      add(id); state.dockRemoved.delete(id);
    }
  }
  // Extra windows of an app (and windows without an app) get their own slot at the end.
  const byApp = {};
  for (const w of [...real].sort((a, b) => winNum(a) - winNum(b))) (byApp[w.app_id || ''] ||= []).push(w);
  const firstOf = new Set(Object.entries(byApp).filter(([k]) => k && dock.includes(k)).map(([, ws]) => ws[0].id));
  for (const w of real) if (!firstOf.has(w.id) && (!w.app_id || dock.includes(w.app_id))) add('win:' + w.id);
  // Drop window slots whose window closed or now sits in its app's slot.
  const live = new Set(real.map(w => w.id));
  const kept = dock.filter(k => !k.startsWith('win:') || (live.has(k.slice(4)) && !firstOf.has(k.slice(4))));
  if (kept.length !== dock.length) { state.pinned = kept; changed = true }

  state.prevRunning = open;
  if (changed) { savePinned(); updateSettingsCounts() }
}

function homeTiles() {
  const wins = runningWindows().sort((a, b) => winNum(a) - winNum(b));
  const used = new Set(), out = [];
  const winTile = (w, slot) => { used.add(w.id); return { slot, html: tileHTML({ id: w.id, kind: 'window', img: w.icon_url, name: windowName(w), running: true, minimized: w.minimized, named: !!w.label, slot }) } };
  const slotted = new Set(state.pinned.filter(k => k.startsWith('win:')).map(k => k.slice(4)));
  for (const key of state.pinned) {
    if (key.startsWith('win:')) { const w = wins.find(x => x.id === key.slice(4)); if (w) out.push(winTile(w, key)); continue }
    if (state.hidden.has('app:' + key)) continue;
    const w = wins.find(x => x.app_id === key && !slotted.has(x.id));
    if (w) out.push(winTile(w, key));
    else { const a = appById(key); if (a) out.push({ slot: key, html: tileHTML({ id: a.id, kind: 'app', img: a.icon_url, name: a.name, slot: key }) }) }
  }
  // Anything not on the dock (e.g. removed from Home while open) is shown last.
  for (const w of wins) if (!used.has(w.id)) out.push(winTile(w, ''));
  return { wins, tiles: out };
}

function renderHome() {
  const all = state.windows.list;
  if (state.windows.supported) updateDock(all);
  const { wins, tiles } = homeTiles();
  const badge = $('#badge-home'); badge.textContent = wins.length; badge.hidden = !wins.length;
  $('#count-running').textContent = wins.length;

  let html = tiles.map(t => t.html).join('');
  if (!html) html = !state.windows.supported
    ? emptyHTML('desktops', 'Windows unavailable', esc(state.windows.reason || 'This desktop does not share its window list.'))
    : emptyHTML('apps', 'Your dock is empty', 'Open an app on the desktop or from Apps, and it stays here.');
  setHTML($('#grid-running'), html);

  // Explain process-only results (no window API yet) once, dismissibly.
  const processOnly = all.some(w => w.process_only);
  const msg = processOnly ? 'Showing running apps. To switch between windows, run <code>./install-gnome-extension.sh</code> and log in again.' : '';
  $('#home-banner-text').innerHTML = msg;
  $('#home-banner').hidden = !msg || state.bannerDismissed === msg;
}

/* ---------- Arrange Home: tap two icons to swap them ---------- */
function setArranging(on) {
  state.arranging = on; state.arrangeSel = '';
  document.body.classList.toggle('arranging', on);
  $('#arrange-bar').hidden = !on;
  $('#arrange-text').textContent = 'Tap an icon, then tap where it should go';
  if (on && state.panel !== 'home') selectPanel('home');
  delete $('#grid-running').dataset.html; renderHome();
}
function arrangeTap(tile) {
  let slot = tile.dataset.slot;
  if (!slot) {                                    // a tile not on the dock yet: give it a slot first
    const w = state.windows.list.find(x => x.id === tile.dataset.id);
    slot = w?.app_id && !state.pinned.includes(w.app_id) ? w.app_id : 'win:' + tile.dataset.id;
    if (!state.pinned.includes(slot)) state.pinned.push(slot);
  }
  if (!state.arrangeSel) { state.arrangeSel = slot; $('#arrange-text').textContent = 'Now tap the icon to swap with' }
  else if (state.arrangeSel === slot) { state.arrangeSel = ''; $('#arrange-text').textContent = 'Tap an icon, then tap where it should go' }
  else {
    const d = state.pinned, a = d.indexOf(state.arrangeSel), b = d.indexOf(slot);
    if (a >= 0 && b >= 0) [d[a], d[b]] = [d[b], d[a]];
    state.arrangeSel = ''; $('#arrange-text').textContent = 'Swapped. Tap two more, or Done';
  }
  savePinned(); delete $('#grid-running').dataset.html; renderHome();
}

/* ---------- Apps ---------- */
const CATEGORIES = [
  ['Internet', ['Network', 'WebBrowser', 'Email', 'Chat', 'InstantMessaging', 'FileTransfer']],
  ['Media', ['AudioVideo', 'Audio', 'Video', 'Player', 'Music']],
  ['Graphics', ['Graphics', 'Photography', '2DGraphics', '3DGraphics']],
  ['Development', ['Development', 'IDE', 'Debugger', 'RevisionControl']],
  ['Office', ['Office', 'WordProcessor', 'Spreadsheet', 'Presentation']],
  ['Games', ['Game']],
  ['Security', ['Security']],
  ['System', ['System', 'Settings', 'Monitor', 'PackageManager', 'HardwareSettings', 'TerminalEmulator']],
  ['Utilities', ['Utility', 'Accessories', 'TextEditor', 'FileManager', 'Archiving']],
];
function categoryOf(app) {
  const cats = (app.categories || '').split(';');
  return CATEGORIES.find(([, keys]) => keys.some(k => cats.includes(k)))?.[0] || 'Other';
}
function renderApps() {
  const q = $('#search').value.trim().toLowerCase();
  $('#search-clear').hidden = !q;
  const visible = state.apps.filter(a => !state.hidden.has('app:' + a.id));
  const running = runningAppIds();
  $('#count-apps').textContent = visible.length;

  // Category chips with live counts
  const counts = { all: visible.length, running: visible.filter(a => running.has(a.id)).length };
  for (const a of visible) { const c = categoryOf(a); counts[c] = (counts[c] || 0) + 1 }
  const chips = [['all', 'All'], ...(counts.running ? [['running', 'Running']] : []), ...[...CATEGORIES.map(([n]) => n), 'Other'].filter(n => counts[n]).map(n => [n, n])];
  if (!chips.some(([k]) => k === state.category)) state.category = 'all';
  setHTML($('#chips'), chips.map(([k, label]) => `<button class="chip${k === state.category ? ' on' : ''}" type="button" data-cat="${esc(k)}">${esc(label)}<b>${counts[k]}</b></button>`).join(''));

  const list = visible.filter(a => {
    if (q && !a.name.toLowerCase().includes(q) && !a.id.toLowerCase().includes(q)) return false;
    if (state.category === 'running') return running.has(a.id);
    return state.category === 'all' || categoryOf(a) === state.category;
  });
  setHTML($('#grid-apps'), list.length
    ? list.map(a => tileHTML({ id: a.id, kind: 'app', img: a.icon_url, name: a.name, running: running.has(a.id) })).join('')
    : state.apps.length ? emptyHTML('search', 'No apps found', q ? `Nothing matches “${esc(q)}”.` : 'Hidden apps can be restored in Settings.') : emptyHTML('apps', 'Loading apps…', ''));
}

/* ---------- Desktops ---------- */
function renderDesktops() {
  const d = state.workspaces, root = $('#screens');
  if (!d) return setHTML(root, emptyHTML('desktops', 'Loading desktops…', ''));
  if (!d.supported) {
    $('#desktops-hint').textContent = '';
    return setHTML(root, emptyHTML('desktops', 'Desktops unavailable', esc(d.message || 'This desktop does not expose workspace controls.')));
  }
  $('#desktops-hint').textContent = d.message || (d.per_monitor ? 'Each screen has its own desktops.' : '');
  setHTML(root, d.screens.map(s => `
    <section class="screen">
      <div class="screen-head"><span class="screen-icon">${icon('desktops')}</span><h3>${esc(s.name)}</h3></div>
      <div class="ws-grid">
        ${s.workspaces.map((w, i) => `<button class="ws${w.active ? ' active' : ''}" type="button" data-monitor="${esc(s.name)}" data-ws="${esc(w.id)}"><strong>${i + 1}</strong><span>${esc(w.name)}</span></button>`).join('')}
        <button class="ws add" type="button" data-new="${esc(s.name)}" aria-label="New desktop on ${esc(s.name)}">${icon('plus')}<span>New</span></button>
      </div>
    </section>`).join('') || emptyHTML('desktops', 'No screens found', ''));
}
$('#screens').addEventListener('click', async e => {
  const ws = e.target.closest('[data-ws]'), add = e.target.closest('[data-new]');
  try {
    if (ws) { await api('workspace', { action: 'switch', monitor: ws.dataset.monitor, workspace: ws.dataset.ws }); toast('Switched desktop') }
    if (add) {
      const per = state.workspaces?.per_monitor;
      const name = per ? prompt(`New desktop on ${add.dataset.new}`, `Deck-${Date.now().toString().slice(-3)}`) : 'new';
      if (!name) return;
      await api('workspace', { action: 'create', monitor: add.dataset.new, workspace: name.trim() }); toast('Desktop created');
    }
  } catch (err) { toast(err.message) }
});

/* ---------- Tile actions: tap, long-press, right-click ---------- */
async function activate(kind, id) {
  try {
    if (kind === 'window') {
      const w = state.windows.list.find(x => x.id === id); if (!w) return;
      // Process-only entries have no window handle: launching again would just open duplicates.
      if (w.process_only) return toast('Window switching needs the GNOME bridge — log out and back in once');
      await api('window/activate', { id });
    } else {
      const win = state.windows.list.find(w => w.app_id === id && !w.process_only);
      if (win) return activate('window', win.id);
      await api('launch', { id }); toast('Launching ' + (appById(id)?.name || 'app'));
    }
  } catch (e) { toast(e.message) }
}

function openMenu(kind, id) {
  const w = kind === 'window' ? state.windows.list.find(x => x.id === id) : null;
  const app = appById(w ? w.app_id : id);
  const name = w ? windowName(w) : app?.name || id;
  const img = w?.icon_url || app?.icon_url;
  $('#menu-icon').innerHTML = img ? `<img src="${esc(img)}" alt="">` : icon('apps');
  $('#menu-name').textContent = name;
  $('#menu-sub').textContent = w ? (w.process_only ? 'Running' : w.label ? `${w.app_name || 'Window'} · ${w.title}` : w.title) : (app ? categoryOf(app) : '');
  const pinned = app && state.pinned.includes(app.id);
  const actions = [
    ['launch', w ? 'Switch to' : 'Open', () => activate(kind, id)],
    ...(app && (w || runningAppIds().has(app.id)) ? [['plus', 'New window', async () => { try { await api('launch', { id: app.id }); toast('Opening new window') } catch (e) { toast(e.message) } }]] : []),
    ...(w && !w.process_only ? [['edit', 'Rename…', () => openRename(w)]] : []),
    ...(app ? [['pin', pinned ? 'Remove from Home' : 'Add to Home', () => togglePin(app.id)]] : []),
    ...(state.panel === 'home' ? [['apps', 'Arrange Home', () => setArranging(true)]] : []),
    ['eye-off', 'Hide from Deck', () => hideItem(w ? winKey(w) : 'app:' + id, name), 'danger'],
  ];
  $('#menu-actions').innerHTML = actions.map(([ic, label, , cls], i) => `<button class="menu-action ${cls || ''}" type="button" data-i="${i}">${icon(ic)}<span>${label}</span></button>`).join('');
  $$('#menu-actions [data-i]').forEach(b => b.onclick = () => { $('#tile-menu').close(); actions[b.dataset.i][2]() });
  showDialog($('#tile-menu'));
}
$('#menu-cancel').onclick = () => $('#tile-menu').close();
$('#tile-menu').addEventListener('click', e => { if (e.target === e.currentTarget) e.currentTarget.close() });

let renaming = null;
function openRename(w) {
  renaming = w;
  $('#rename-hint').textContent = `${w.app_name || 'Window'} · ${w.title}`;
  $('#rename-input').value = w.label || '';
  $('#rename-input').placeholder = w.title || 'Name shown on the deck';
  $('#rename-reset').hidden = !w.label;
  $('#rename').showModal();
  setTimeout(() => { $('#rename-input').focus(); $('#rename-input').select() }, 60);
}
async function saveRename(name) {
  const w = renaming; if (!w) return;
  try {
    await api('window/rename', { id: w.id, name });
    w.label = name.trim();                       // optimistic; the live stream confirms it
    renderHome(); renderSheetWindows();
    toast(name.trim() ? `Renamed to “${name.trim()}”` : 'Name cleared');
  } catch (e) { toast(e.message) }
  renaming = null;
}
$('#rename-form').addEventListener('submit', e => {
  if (e.submitter?.value !== 'save') return;
  e.preventDefault(); $('#rename').close(); saveRename($('#rename-input').value);
});
$('#rename-reset').onclick = () => { $('#rename').close(); saveRename('') };

function togglePin(appId) {
  const i = state.pinned.indexOf(appId);
  if (i >= 0) { state.pinned.splice(i, 1); state.dockRemoved.add(appId) }
  else { state.pinned.push(appId); state.dockRemoved.delete(appId) }
  savePinned(); renderHome(); updateSettingsCounts();
  toast(i >= 0 ? 'Removed from Home' : 'Added to Home');
}
function hideItem(key, name) {
  state.hidden.add(key); saveHidden();
  renderHome(); renderApps(); renderSheetWindows(); updateSettingsCounts();
  toast(`Hidden ${name}`);
}

// One delegated handler for every tile grid: tap = open, hold/right-click = menu.
(function bindTiles() {
  let timer = null, held = false, startX = 0, startY = 0, pressed = null;
  const cancel = () => { clearTimeout(timer); timer = null; pressed?.classList.remove('pressing'); pressed = null };
  document.addEventListener('pointerdown', e => {
    const tile = e.target.closest('.tile'); if (!tile || e.button > 0) return;
    if (state.arranging && tile.closest('#grid-running')) return;
    held = false; startX = e.clientX; startY = e.clientY; pressed = tile;
    timer = setTimeout(() => { held = true; tile.classList.remove('pressing'); navigator.vibrate?.(10); openMenu(tile.dataset.kind, tile.dataset.id); cancel() }, 480);
    setTimeout(() => timer && tile.classList.add('pressing'), 150);
  });
  document.addEventListener('pointermove', e => { if (timer && Math.hypot(e.clientX - startX, e.clientY - startY) > 10) cancel() });
  document.addEventListener('pointerup', cancel); document.addEventListener('pointercancel', cancel);
  document.addEventListener('click', e => {
    const tile = e.target.closest('.tile'); if (!tile) return;
    if (held) { held = false; e.preventDefault(); return }
    if (state.arranging && tile.closest('#grid-running')) { arrangeTap(tile); return }
    if (tile.closest('#sheet')) $('#sheet').close();
    activate(tile.dataset.kind, tile.dataset.id);
  });
  document.addEventListener('contextmenu', e => {
    const tile = e.target.closest('.tile'); if (!tile) return;
    e.preventDefault(); cancel(); if (state.arranging) return; openMenu(tile.dataset.kind, tile.dataset.id);
  });
})();

/* ---------- Media ---------- */
function currentPlayer() {
  const ps = state.media.players || [];
  return ps.find(p => p.id === state.player) || ps.find(p => p.id === state.media.active) || null;
}
function livePosition(p) {
  if (!p) return 0;
  const pos = p.position + (p.playing ? (performance.now() - state.mediaAt) / 1000 : 0);
  return p.length ? Math.min(pos, p.length) : pos;
}
function playerApp(p) {
  const base = (p?.name || '').toLowerCase(); if (!base) return null;
  const id = a => a.id.toLowerCase().replace(/\.desktop$/, '');
  return state.apps.find(a => id(a) === base || id(a).split('.').pop() === base)
    || state.apps.filter(a => id(a).includes(base) || a.name.toLowerCase().includes(base)).sort((a, b) => a.id.length - b.id.length)[0] || null;
}
const playerName = p => playerApp(p)?.name || (p?.name ? p.name[0].toUpperCase() + p.name.slice(1) : 'Player');
function artUrl(p) {
  if (!p?.art_url) return '';
  return p.art_url.startsWith('/') ? `${p.art_url}&token=${encodeURIComponent(state.token)}&v=${encodeURIComponent(p.title || '')}` : p.art_url;
}
const artCache = new Map();
function setArt(el, url) {
  if (el.dataset.url === url) return;
  el.dataset.url = url;
  const apply = ok => { if (el.dataset.url !== url) return; el.style.backgroundImage = ok ? `url("${url.replace(/"/g, '%22')}")` : ''; el.classList.toggle('has-img', ok) };
  if (!url) return apply(false);
  if (artCache.has(url)) return apply(artCache.get(url));
  const img = new Image();
  img.onload = () => { artCache.set(url, true); apply(true) };
  img.onerror = () => { artCache.set(url, false); apply(false) };
  img.src = url;
}

function applyMedia(m) {
  state.media = m || { available: false, players: [] };
  state.mediaAt = performance.now();
  if (state.player && !state.media.players?.some(p => p.id === state.player)) state.player = '';
  renderMedia(); renderPlayers();
}
function renderMedia() {
  const m = state.media, p = currentPlayer();
  const title = p?.title || (p ? 'Nothing playing' : m.available ? 'No player running' : 'Media controls unavailable');
  const sub = p ? [p.artist || p.album, playerName(p)].filter(Boolean).join(' · ') : m.available ? 'Start music on your desktop' : 'Install playerctl on the desktop';
  $('#mini-title').textContent = title;
  $('#mini-sub').textContent = sub;
  $('#sheet-title').textContent = title;
  $('#sheet-sub').textContent = p ? [p.artist, p.album].filter(Boolean).join(' · ') : sub;
  $('#sheet-source').textContent = p ? playerName(p) : 'Now playing';
  for (const b of $$('[data-media="play-pause"]')) {
    b.innerHTML = icon(p?.playing ? 'pause' : 'play');
    b.setAttribute('aria-label', p?.playing ? 'Pause' : 'Play');
  }
  $('#badge-music').hidden = !p?.playing;
  const url = artUrl(p);
  setArt($('#mini-art'), url); setArt($('#sheet-art'), url); setArtBackground(url);
  const canSeek = !!(p && p.length > 0), hasVol = p && typeof p.volume === 'number';
  seekSliders.forEach(s => s.el.classList.toggle('disabled', !canSeek));
  volSliders.forEach(s => s.el.classList.toggle('disabled', !hasVol));
  $$('[data-mute]').forEach(b => { b.disabled = !hasVol; b.innerHTML = icon(hasVol && p.volume === 0 ? 'volume-off' : 'volume') });
  $$('[data-media]').forEach(b => b.disabled = !p);
  tick();
}
function renderPlayers() {
  const cur = currentPlayer();
  const ps = (state.media.players || []).filter(p => p.title || p.id === cur?.id)
    .sort((a, b) => (b.playing - a.playing) || ((b.status === 'Paused') - (a.status === 'Paused')));
  $('#sheet-side').hidden = ps.length < 2;
  setHTML($('#players'), ps.length ? ps.map(p => {
    const app = playerApp(p);
    const pic = app?.icon_url ? `<img class="player-icon" src="${esc(app.icon_url)}" alt="">` : `<span class="player-icon">${icon('music')}</span>`;
    const state_ = p.playing ? 'Playing' : p.status;
    return `<button class="player${cur?.id === p.id ? ' active' : ''}" type="button" data-player="${esc(p.id)}">${pic}<span class="player-text"><strong>${esc(playerName(p))}</strong><span>${esc(p.title || 'Idle')}</span></span><span class="player-state${p.playing ? ' on' : ''}">${esc(state_)}</span></button>`;
  }).join('') : emptyHTML('music', 'No players', 'Play something on your desktop.'));
}
$('#players').addEventListener('click', e => {
  const b = e.target.closest('[data-player]'); if (!b) return;
  state.player = b.dataset.player; renderMedia(); renderPlayers();
});
function renderSheetWindows() { }  // open apps are shown on Home only

async function mediaAction(action, extra = {}) {
  const p = currentPlayer();
  try { const r = await api('media/action', { action, player: p?.id || '', ...extra }); if (r.media) applyMedia(r.media) }
  catch (e) { toast(e.message) }
}
function seekTo(seconds) {
  const p = currentPlayer(); if (!p?.length) return;
  p.position = Math.max(0, Math.min(p.length - 0.5, seconds)); state.mediaAt = performance.now(); tick();
  mediaAction('seek', { position: p.position });
}
let lastVolume = 1;
function setVolume(v) {
  const p = currentPlayer(); if (!p || typeof p.volume !== 'number') return;
  if (v > 0) lastVolume = v;
  p.volume = v; renderMedia();
  mediaAction('volume', { volume: v });
}
$$('[data-media]').forEach(b => b.addEventListener('click', () => {
  const a = b.dataset.media;
  if (a === 'back10' || a === 'fwd10') return seekTo(livePosition(currentPlayer()) + (a === 'back10' ? -10 : 10));
  mediaAction(a);
}));
$$('[data-mute]').forEach(b => b.addEventListener('click', () => { const p = currentPlayer(); if (p) setVolume(p.volume > 0 ? 0 : lastVolume || 1) }));

/* ---------- Sliders: tap anywhere or drag ---------- */
function makeSlider(el, { onPreview, onCommit, step = .05 }) {
  const s = { el, dragging: false, value: 0 };
  const set = f => { s.value = Math.max(0, Math.min(1, f)); el.style.setProperty('--p', (s.value * 100).toFixed(2) + '%') };
  const at = e => { const r = el.getBoundingClientRect(); return (e.clientX - r.left) / r.width };
  el.addEventListener('pointerdown', e => {
    if (el.classList.contains('disabled')) return;
    e.preventDefault(); el.setPointerCapture(e.pointerId);
    s.dragging = true; el.classList.add('dragging'); set(at(e)); onPreview?.(s.value);
  });
  el.addEventListener('pointermove', e => { if (s.dragging) { set(at(e)); onPreview?.(s.value) } });
  const end = e => {
    if (!s.dragging) return;
    s.dragging = false; el.classList.remove('dragging');
    if (e.type === 'pointerup') onCommit(s.value); else renderMedia();
  };
  el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
  el.addEventListener('keydown', e => {
    const d = { ArrowLeft: -step, ArrowDown: -step, ArrowRight: step, ArrowUp: step }[e.key];
    if (d) { e.preventDefault(); set(s.value + d); onCommit(s.value) }
  });
  s.set = f => { if (!s.dragging) set(f) };
  return s;
}
const previewTime = f => { const p = currentPlayer(); if (p?.length) $$('[data-pos]').forEach(t => t.textContent = fmt(f * p.length)) };
const seekSliders = ['#mini-seek', '#mini-seek-wide', '#sheet-seek'].map(id => makeSlider($(id), { onPreview: previewTime, onCommit: f => seekTo(f * (currentPlayer()?.length || 0)), step: .02 }));
const volSliders = ['#mini-volume', '#sheet-volume'].map(id => makeSlider($(id), { onPreview: f => volSliders.forEach(s => s.set(f)), onCommit: setVolume }));

function tick() {
  const p = currentPlayer(), len = p?.length || 0, pos = livePosition(p);
  const dragging = seekSliders.some(s => s.dragging);
  seekSliders.forEach(s => {
    s.set(len ? pos / len : 0);
    s.el.setAttribute('aria-valuetext', `${fmt(pos)} of ${fmt(len)}`);
  });
  if (!dragging) $$('[data-pos]').forEach(t => t.textContent = fmt(pos));
  $$('[data-len]').forEach(t => t.textContent = fmt(len));
  $$('[data-left]').forEach(t => t.textContent = len ? '-' + fmt(len - pos) : '0:00');
  if (p && typeof p.volume === 'number') volSliders.forEach(s => s.set(p.volume));
}
setInterval(() => { if (currentPlayer()?.playing) tick() }, 250);

/* ---------- Artwork background ---------- */
let artBg = store.get('deck-art-bg', false);
function setArtBackground(url) {
  const bg = $('#sheet-bg'), on = artBg && !!url && artCache.get(url) !== false;
  bg.style.backgroundImage = on ? `url("${url.replace(/"/g, '%22')}")` : '';
  $('#sheet').classList.toggle('art-bg', on);
}
function toggleArtBackground() {
  artBg = !artBg; store.set('deck-art-bg', artBg);
  setArtBackground($('#sheet-art').dataset.url || '');
  toast(artBg ? 'Artwork background on' : 'Artwork background off');
}
$('#sheet-art').addEventListener('click', toggleArtBackground);
$('#sheet-art').addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleArtBackground() } });

/* ---------- Sheet ---------- */
function openSheet() { renderPlayers(); renderSheetWindows(); showDialog($('#sheet')) }
$('#mini-open').onclick = openSheet; $('#mini-expand').onclick = openSheet; $('#nav-music').onclick = openSheet;
$('#sheet-close').onclick = () => $('#sheet').close();

/* ---------- Navigation ---------- */
function selectPanel(name) {
  if (name !== 'home' && state.arranging) { state.arranging = false; state.arrangeSel = ''; document.body.classList.remove('arranging'); $('#arrange-bar').hidden = true }
  state.panel = name;
  $$('.nav-item[data-panel]').forEach(b => b.classList.toggle('active', b.dataset.panel === name));
  for (const p of ['home', 'apps', 'desktops']) $('#panel-' + p).hidden = p !== name;
  $('#main').scrollTop = 0;
  if (name === 'apps' && !state.apps.length) loadApps();
}
$$('.nav-item[data-panel]').forEach(b => b.onclick = () => selectPanel(b.dataset.panel));
$('#chips').addEventListener('click', e => { const c = e.target.closest('[data-cat]'); if (c) { state.category = c.dataset.cat; renderApps(); $('#main').scrollTop = 0 } });
$('#search').addEventListener('input', renderApps);
$('#search-clear').onclick = () => { $('#search').value = ''; renderApps(); $('#search').focus() };
$('#arrange-done').onclick = () => setArranging(false);
$('#arrange-btn').onclick = () => setArranging(!state.arranging);
$('#home-banner-close').onclick = () => { state.bannerDismissed = $('#home-banner-text').innerHTML; store.set('deck-banner-dismissed', state.bannerDismissed); $('#home-banner').hidden = true };

/* ---------- Settings ---------- */
function applyPrefs() {
  const b = document.body;
  b.classList.toggle('labels-off', !state.prefs.labels);
  b.classList.remove('size-s', 'size-m', 'size-l'); b.classList.add('size-' + state.prefs.size);
  $('#mini').hidden = !state.prefs.media; $('#nav-music').hidden = !state.prefs.media;
  $('.nav-item[data-panel="apps"]').hidden = !state.prefs.apps;
  $('.nav-item[data-panel="desktops"]').hidden = !state.prefs.desktops;
  if ((state.panel === 'apps' && !state.prefs.apps) || (state.panel === 'desktops' && !state.prefs.desktops)) selectPanel('home');
  $$('#pref-size button').forEach(x => x.classList.toggle('on', x.dataset.size === state.prefs.size));
}
function updateSettingsCounts() { $('#hidden-count').textContent = state.hidden.size; $('#pinned-count').textContent = state.pinned.length }
function openSettings() {
  for (const k of ['labels', 'media', 'apps', 'desktops']) $('#pref-' + k).checked = !!state.prefs[k];
  updateSettingsCounts(); applyPrefs(); showDialog($('#settings'));
}
$('#settings-open').onclick = openSettings; $('#rail-settings').onclick = openSettings;
for (const k of ['labels', 'media', 'apps', 'desktops']) $('#pref-' + k).addEventListener('change', e => { state.prefs[k] = e.target.checked; savePrefs(); applyPrefs() });
$('#pref-size').addEventListener('click', e => { const b = e.target.closest('[data-size]'); if (b) { state.prefs.size = b.dataset.size; savePrefs(); applyPrefs() } });
$('#restore-hidden').onclick = () => { state.hidden.clear(); saveHidden(); renderHome(); renderApps(); renderSheetWindows(); updateSettingsCounts(); toast('Hidden items restored') };
$('#clear-pins').onclick = () => { state.pinned = []; state.dockRemoved.clear(); state.prevRunning = null; savePinned(); renderHome(); updateSettingsCounts(); toast('Home dock reset') };
$('#re-pair').onclick = () => { $('#settings').close(); openPair() };
$('#status-pill').onclick = $('#rail-status').onclick = () => state.conn === 'unpaired' ? openPair() : (toast(state.conn === 'live' ? 'Live connection' : 'Reconnecting…'), state.conn !== 'live' && connect());

/* ---------- Pairing ---------- */
function openPair() {
  if ($('#pair').open) return;
  $('#pin').value = ''; $('#pair-error').hidden = true;
  $('#pair').showModal(); setTimeout(() => $('#pin').focus(), 50);
}
async function submitPin() {
  const pin = $('#pin').value.trim();
  if (!/^\d{6}$/.test(pin)) { $('#pair-error').textContent = 'Enter all 6 digits.'; $('#pair-error').hidden = false; return }
  const prev = state.token; state.token = pin;
  try {
    const r = await fetch('/api/session', { headers: { Authorization: 'Bearer ' + pin } });
    if (!r.ok) throw 0;
    localStorage.setItem('deck-token', pin); $('#pair').close(); toast('Paired'); connect();
  } catch {
    state.token = prev; $('#pair-error').textContent = 'That PIN didn’t work. Check the terminal and try again.'; $('#pair-error').hidden = false;
    $('#pin').select();
  }
}
$('#pair-form').addEventListener('submit', e => { if (e.submitter?.value === 'connect') { e.preventDefault(); submitPin() } });
$('#pin').addEventListener('input', e => { e.target.value = e.target.value.replace(/\D/g, '').slice(0, 6); if (e.target.value.length === 6) submitPin() });

/* ---------- Full screen (not available on iPhone) ---------- */
const root = document.documentElement;
if (!(root.requestFullscreen || root.webkitRequestFullscreen)) $('#rail-fullscreen').hidden = true;
$('#rail-fullscreen').onclick = () => {
  if (document.fullscreenElement || document.webkitFullscreenElement) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  else (root.requestFullscreen || root.webkitRequestFullscreen).call(root)?.catch?.(() => { });
};

/* ---------- Keyboard (desktop browsers) ---------- */
document.addEventListener('keydown', e => {
  if (e.target.matches('input') || document.querySelector('dialog[open]:not(#sheet)')) return;
  if (e.key === '/') { e.preventDefault(); selectPanel('apps'); $('#search').focus() }
  else if (e.key === ' ' && !e.target.closest('button,.slider')) { e.preventDefault(); mediaAction('play-pause') }
  else if (e.key === '1') selectPanel('home'); else if (e.key === '2') selectPanel('apps'); else if (e.key === '3') selectPanel('desktops');
});

/* ---------- Boot ---------- */
applyPrefs(); renderHome(); renderApps(); renderDesktops(); renderMedia(); renderPlayers(); updateSettingsCounts();
connect();
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => { });
