let S = null;          // latest /api/status
let pendingTarget = null, targetTimer = null;
const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const opts = body === undefined ? {} : {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)};
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = '/login'; throw new Error('login'); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { toast(j.detail || 'Something went wrong'); throw new Error(j.detail); }
  return j;
}

function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', 3500);
}

const ago = (ts) => {
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

// ---------- tabs ----------
document.querySelectorAll('nav button').forEach((b) => b.onclick = () => {
  document.querySelectorAll('nav button').forEach((x) => x.classList.toggle('active', x === b));
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.id === 'tab-' + b.dataset.tab));
  $('title').textContent = b.dataset.tab[0].toUpperCase() + b.dataset.tab.slice(1);
  if (b.dataset.tab === 'history') { loadHistory(24); loadEvents(); }
  if (b.dataset.tab === 'setup') { loadSetup(); loadVersion(); }
});

// ---------- home ----------
async function load() {
  S = await api('/api/status');
  render();
}

function render() {
  const h = S.heating || {};
  $('updated').textContent = h.updated ? 'Updated ' + ago(h.updated) : '';
  $('heat-none').style.display = h.connected ? 'none' : 'block';
  $('heat-ok').style.display = h.connected ? 'block' : 'none';
  if (h.connected) {
    $('indoor').textContent = h.indoor_temp != null ? h.indoor_temp.toFixed(1) + '°' : '–';
    if (pendingTarget === null) $('target').textContent = h.target_temp != null ? h.target_temp.toFixed(1) + '°' : '–';
    $('boiler').textContent = h.boiler_on ? '🔥 Heating' : 'Idle';
    $('boiler').className = 'pill' + (h.boiler_on ? ' on' : '');
    const modes = {SCHEDULE: 'On schedule', MANUAL: 'Manual', OFF: 'Off', BOOST: `Boost · ${h.boost_minutes} min left`};
    $('mode').textContent = modes[h.mode] || h.mode || '';
    $('boost-off').style.display = h.mode === 'BOOST' ? 'inline-block' : 'none';
  }
  if (S.weather) $('outdoor').textContent = `Outside ${S.weather.temp.toFixed(1)}°`;

  const errs = Object.entries(S.errors || {});
  $('errors').innerHTML = errs.length ? `<div class="card err">${errs.map(([k, v]) => `<b>${esc(k)}</b>: ${esc(v)}`).join('<br>')}</div>` : '';

  $('people').innerHTML = Object.entries(S.presence).map(([name, p]) =>
    `<button class="pill ${p.home ? 'home' : 'away'}" onclick="togglePerson('${esc(name)}', ${!p.home})">${esc(name)} · ${p.home ? 'home' : 'out'} <span class="muted">${ago(p.since)}</span></button>`).join('');
  $('hold').textContent = S.hold ? 'On' : 'Off';
  $('hold').className = S.hold ? 'primary' : '';

  const kids = S.devices.filter((d) => d.kids && !d.hidden);
  $('kids-card').style.display = kids.length ? 'block' : 'none';
  $('kids-lights').innerHTML = kids.map(deviceRow).join('');
  $('devices').innerHTML = S.tuya_configured ? devicesHtml() : "Smart Life isn't connected yet – go to Setup.";
}

let showHidden = false;
function devicesHtml() {
  if (!S.devices.length) return 'No devices found in your Smart Life account.';
  const visible = S.devices.filter((d) => !d.hidden), hidden = S.devices.filter((d) => d.hidden);
  const homes = [...new Set(S.devices.map((d) => d.home))];
  let html = '';
  for (const home of homes) {
    const ds = visible.filter((d) => d.home === home);
    if (!ds.length) continue;
    html += `<div class="row" style="margin-top:6px"><b>${esc(home || 'Devices')}</b>` +
      (homes.length > 1 ? `<button class="small" onclick="hideHome('${esc(home)}', true)">Hide this home</button>` : '') + `</div>`;
    html += ds.map((d) => deviceRow(d, true)).join('');
  }
  if (!visible.length) html += '<p class="muted">All devices are hidden.</p>';
  if (hidden.length) {
    html += `<div style="margin-top:14px"><button class="small" onclick="showHidden=!showHidden;render()">${showHidden ? 'Hide' : 'Show'} hidden devices (${hidden.length})</button></div>`;
    if (showHidden) html += hidden.map((d) => `<div class="device row"><span class="muted">${esc(d.name)}${d.home ? ' · ' + esc(d.home) : ''}</span>
      <button class="small" onclick="hideDev('${d.id}', false)">Unhide</button></div>`).join('');
  }
  return html;
}
async function hideDev(id, hidden) { S.devices = await api(`/api/devices/${id}/hidden`, {hidden}); render(); }
async function hideHome(home, hidden) { S.devices = await api('/api/homes/hidden', {home, hidden}); render(); toast(`Hid all devices in ${home}`); }

function deviceRow(d, full) {
  const btns = d.kind === 'light'
    ? `<button class="small" onclick="dev('${d.id}','on')">On</button>
       <button class="small" onclick="dev('${d.id}','off')">Off</button>
       <button class="small red" onclick="dev('${d.id}','red')">Night red</button>
       <button class="small" onclick="dev('${d.id}','white')">White</button>`
    : `<button class="small" onclick="dev('${d.id}','on')">On</button>
       <button class="small" onclick="dev('${d.id}','off')">Off</button>`;
  const kidsToggle = full
    ? `<div class="row" style="margin-top:6px">` +
      (d.kind === 'light' ? `<label class="muted"><input type="checkbox" style="width:auto;margin:0 6px 0 0" ${d.kids ? 'checked' : ''} onchange="markKids('${d.id}', this.checked)">Kids' light</label>` : '<span></span>') +
      `<button class="small" onclick="hideDev('${d.id}', true)">Hide</button></div>` : '';
  const state = !d.online ? 'offline' : d.on ? (d.mode === 'colour' ? 'on · colour' : 'on') : 'off';
  return `<div class="device"><div class="row"><b>${esc(d.name)}</b><span class="pill ${d.on ? 'on' : ''}">${state}${d.power_w != null ? ' · ' + d.power_w + ' W' : ''}</span></div>
    <div class="wrap" style="margin-top:8px">${btns}</div>${kidsToggle}</div>`;
}

async function dev(id, action) { S.devices = await api('/api/devices/' + id, {action}); render(); }
async function markKids(id, kids) { S.devices = await api(`/api/devices/${id}/kids`, {kids}); render(); }
async function togglePerson(person, home) { S.presence = await api('/api/presence', {person, home}); render(); }
async function toggleHold() { const r = await api('/api/hold', {on: !S.hold}); S.hold = r.hold; render(); }

function nudge(delta) {
  const base = pendingTarget ?? S.heating.target_temp ?? 19;
  pendingTarget = Math.max(5, Math.min(25, Math.round((base + delta) * 2) / 2));
  $('target').textContent = pendingTarget.toFixed(1) + '°';
  clearTimeout(targetTimer);
  targetTimer = setTimeout(async () => {
    const t = pendingTarget;
    try { S.heating = await api('/api/heating/target', {temp: t}); toast(`Target set to ${t}°`); }
    finally { pendingTarget = null; render(); }
  }, 1200);
}
async function boost(minutes) {
  const temp = Math.max((S.heating.target_temp || 19) + 1, 20);
  S.heating = await api('/api/heating/boost', {minutes, temp}); render(); toast(`Boosting to ${temp}° for ${minutes} min`);
}
async function setMode(mode) { S.heating = await api('/api/heating/mode', {mode}); render(); }

// ---------- history ----------
async function loadHistory(hours) {
  const rows = await api('/api/history?hours=' + hours);
  if (rows.length < 3) { $('chart').innerHTML = 'Collecting data – readings are saved every 5 minutes.'; return; }
  const W = 360, H = 200, P = 26;
  const t0 = rows[0].ts, t1 = rows[rows.length - 1].ts;
  const vals = rows.flatMap((r) => [r.indoor_temp, r.target_temp, r.outdoor_temp]).filter((v) => v != null);
  const lo = Math.floor(Math.min(...vals) - 1), hi = Math.ceil(Math.max(...vals) + 1);
  const x = (t) => P + (t - t0) / Math.max(1, t1 - t0) * (W - P - 4);
  const y = (v) => H - 18 - (v - lo) / (hi - lo) * (H - 28);
  const line = (key, color, dash) => {
    const pts = rows.filter((r) => r[key] != null).map((r) => `${x(r.ts).toFixed(1)},${y(r[key]).toFixed(1)}`);
    return pts.length ? `<polyline fill="none" stroke="${color}" stroke-width="2" ${dash ? 'stroke-dasharray="4 3"' : ''} points="${pts.join(' ')}"/>` : '';
  };
  let bars = '';
  rows.forEach((r, i) => {
    if (r.boiler_on && rows[i + 1]) bars += `<rect x="${x(r.ts)}" y="8" width="${Math.max(1, x(rows[i + 1].ts) - x(r.ts))}" height="${H - 26}" fill="rgba(255,138,61,.18)"/>`;
  });
  let grid = '';
  for (let v = lo; v <= hi; v += Math.max(1, Math.round((hi - lo) / 5))) {
    grid += `<line x1="${P}" x2="${W}" y1="${y(v)}" y2="${y(v)}" stroke="#2a2e38"/><text x="2" y="${y(v) + 4}" fill="#9aa1ad" font-size="10">${v}°</text>`;
  }
  const fmt = (ts) => new Date(ts * 1000).toLocaleString('en-GB', hours > 24 ? {weekday: 'short', hour: '2-digit'} : {hour: '2-digit', minute: '2-digit'});
  grid += `<text x="${P}" y="${H - 2}" fill="#9aa1ad" font-size="10">${fmt(t0)}</text><text x="${W - 4}" y="${H - 2}" fill="#9aa1ad" font-size="10" text-anchor="end">${fmt(t1)}</text>`;
  $('chart').innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${bars}${grid}${line('target_temp', '#777', true)}${line('outdoor_temp', '#4da3ff')}${line('indoor_temp', '#ff8a3d')}</svg>`;
  let on = 0;
  rows.forEach((r, i) => { if (r.boiler_on && rows[i + 1]) on += rows[i + 1].ts - r.ts; });
  $('boiler-hours').textContent = `Boiler ran for about ${(on / 3600).toFixed(1)} hours in this period.`;
}

async function loadEvents() {
  const ev = await api('/api/events');
  $('events').innerHTML = ev.length ? ev.map((e) => {
    let d = e.detail; try { const j = JSON.parse(d); d = Object.entries(j).map(([k, v]) => `${k}: ${v}`).join(', '); } catch (_) {}
    return `<div style="padding:4px 0"><span class="muted">${ago(e.ts)}</span> · ${esc(e.kind)} · ${esc(d)}</div>`;
  }).join('') : 'Nothing yet.';
}

// ---------- setup ----------
async function loadSetup() {
  const s = await api('/api/setup');
  $('hive-done').style.display = s.hive_configured ? 'block' : 'none';
  $('hive-form').style.display = s.hive_configured ? 'none' : 'block';
  if (s.hive_configured) loadZones();
  $('tuya-done').style.display = s.tuya_configured ? 'block' : 'none';
  $('hooks').innerHTML = Object.entries(s.webhooks).map(([p, u]) =>
    `<b>${esc(p)}</b><div class="muted">Arrive</div><div class="url">${esc(u.arrive)}</div><div class="muted">Leave</div><div class="url">${esc(u.leave)}</div>`).join('');
}

async function loadVersion() {
  const v = await api('/api/version');
  const st = v.status && v.status.checked ? ` · last checked ${ago(v.status.checked)} (${v.status.result})` : '';
  $('version').textContent = `Version ${v.commit}${st}`;
  return v;
}

async function checkUpdates() {
  const btn = $('update-btn'); btn.disabled = true;
  const before = (await api('/api/update', {})).commit;
  $('update-msg').textContent = 'Checking GitHub…';
  const started = Math.floor(Date.now() / 1000) - 5;
  for (let i = 0; i < 40; i++) {           // up to ~2 minutes (installing new packages can take a while)
    await new Promise((r) => setTimeout(r, 3000));
    let v;
    try { v = await (await fetch('/api/version')).json(); } catch (_) { $('update-msg').textContent = 'Restarting with the new version…'; continue; }
    if (v.commit && v.commit !== before) { $('update-msg').textContent = `Updated to ${v.commit} – reloading`; setTimeout(() => location.reload(), 1200); return; }
    if (v.status && v.status.checked >= started && v.status.result !== 'Updated') {
      $('update-msg').textContent = v.status.result; btn.disabled = false; loadVersion(); return;
    }
  }
  $('update-msg').textContent = 'No answer yet – try again in a minute.'; btn.disabled = false;
}

async function loadZones() {
  const zs = await api('/api/setup/hive/zones');
  $('hive-zones').innerHTML = zs.length ? zs.map((z) => `<div class="device row">
      <div><b>${z.chosen ? '✓ ' : ''}${esc(z.name)}${z.home ? ' · ' + esc(z.home) : ''}</b><div class="muted">${z.indoor_temp ?? '–'}° now · target ${z.target_temp ?? '–'}° · ${esc(z.mode || '')}${z.online ? '' : ' · OFFLINE'}${z.group ? ' · group' : ''}</div></div>
      ${z.chosen ? '' : `<button class="small" onclick="pickZone('${z.id}')">Use this</button>`}</div>`).join('') : 'No thermostats found.';
}
async function pickZone(id) { await api('/api/setup/hive/zone', {id}); await loadZones(); await api('/api/refresh', {}); load(); toast('Thermostat updated'); }

async function hiveLogin() {
  $('hive-msg').textContent = '';
  try {
    const r = await api('/api/setup/hive/login', {username: $('hive-user').value, password: $('hive-pass').value});
    if (r.status === 'sms_required') { $('hive-sms').style.display = 'block'; $('hive-code').focus(); }
  } catch (e) { $('hive-msg').textContent = e.message; }
}
async function hiveSms() {
  $('hive-msg').textContent = '';
  try {
    await api('/api/setup/hive/sms', {code: $('hive-code').value});
    toast('Hive connected 🎉'); $('hive-sms').style.display = 'none'; loadSetup(); load();
  } catch (e) { $('hive-msg').textContent = e.message; }
}

let qrPoll = null;
async function tuyaStart() {
  $('tuya-msg').textContent = '';
  try {
    const r = await api('/api/setup/tuya/start', {code: $('tuya-code').value});
    $('qr').innerHTML = r.svg; $('qr-box').style.display = 'block'; $('qr-status').textContent = 'Waiting for scan…';
    clearInterval(qrPoll);
    let tries = 0;
    qrPoll = setInterval(async () => {
      if (++tries > 60) { clearInterval(qrPoll); $('qr-status').textContent = 'Expired – tap Show QR code again.'; return; }
      const p = await api('/api/setup/tuya/poll');
      if (p.done) {
        clearInterval(qrPoll); $('qr-box').style.display = 'none';
        toast(`Smart Life connected – ${p.devices.length} devices found`); loadSetup(); load();
      }
    }, 3000);
  } catch (e) { $('tuya-msg').textContent = e.message; }
}

// ---------- start ----------
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
load();
setInterval(() => { if (!document.hidden && pendingTarget === null) load(); }, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
