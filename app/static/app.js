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
function showTab(tab) {
  document.querySelectorAll('nav button').forEach((x) => x.classList.toggle('active', x.dataset.tab === tab));
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.id === 'tab-' + tab));
  $('title').textContent = tab[0].toUpperCase() + tab.slice(1);
  window.scrollTo(0, 0);
  onTab(tab);
}
document.querySelectorAll('nav button').forEach((b) => b.onclick = () => showTab(b.dataset.tab));
function onTab(tab) {
  const b = {dataset: {tab}};
  if (tab === 'summary') { loadLearning(); loadSummary(); loadReport(); loadComfort(); loadThermal(); loadSmartReport(); }
  if (b.dataset.tab === 'history') { loadRoutine(); loadHistory(24); loadEvents(); }
  if (b.dataset.tab === 'energy') loadEnergy(30);
  if (tab === 'devices') { loadAppliances(false); loadOverview(); }
  if (b.dataset.tab === 'setup') { loadSetup(); loadVersion(); loadTariffForm(); loadRouter(); loadPush(); }
}

// ---------- home ----------
async function load() {
  S = await api('/api/status');
  render();
  loadComfort();
  loadThermal();
}

async function loadSummary() {
  try {
    const items = await api('/api/insights');
    const icon = {todo: '⚠️', tip: '💡', good: '✅'};
    const head = {todo: 'To do', tip: 'You could', good: 'Going well'};
    let html = '', last = null;
    for (const i of items) {
      if (i.level !== last) { html += `<div class="muted" style="margin:${last ? 12 : 0}px 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.05em">${head[i.level]}</div>`; last = i.level; }
      html += `<div style="padding:6px 0"><div>${icon[i.level]} <b>${esc(i.title)}</b></div>${i.detail ? `<div class="muted">${esc(i.detail)}</div>` : ''}</div>`;
    }
    $('summary').innerHTML = html;
    if (!items.length) $('summary').textContent = 'Nothing to report yet.';
  } catch (_) {}
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
  if (S.weather) {
    const w = S.weather;
    $('outdoor').innerHTML = `Outside ${w.temp.toFixed(1)}°` + (w.feels_like != null ? ` (feels ${Math.round(w.feels_like)}°)` : '') +
      (w.wind != null ? `<br>Wind ${Math.round(w.wind)} mph ${w.wind_dir || ''}${w.gusts > w.wind + 10 ? `, gusts ${Math.round(w.gusts)}` : ''}` : '') +
      (w.solar > 50 ? ` · ☀️ sun` : '');
  }

  // Home only shows problems that affect it right now (heating / lights). Others appear on the Summary tab.
  const errs = Object.entries(S.errors || {}).filter(([k]) => k === 'hive' || k === 'tuya');
  $('errors').innerHTML = errs.length ? `<div class="card err">${errs.map(([k, v]) => `<b>${esc(k)}</b>: ${esc(v)}`).join('<br>')}</div>` : '';

  $('people').innerHTML = Object.entries(S.presence).map(([name, p]) =>
    `<button class="pill ${p.home ? 'home' : 'away'}" onclick="togglePerson('${esc(name)}', ${!p.home})">${esc(name)} · ${p.home ? 'home' : 'out'} <span class="muted">${ago(p.since)}</span>${whereabouts(name, p)}</button>`).join('');
  $('hold').textContent = S.hold ? 'On' : 'Off';
  $('hold').className = S.hold ? 'primary' : '';
  if (S.hold) $('people').innerHTML += '<span class="pill home">Guests staying</span>';

  const ls = S.lights_signal || {};
  const anyPhone = Object.values(S.presence).some((p) => p.home);
  $('lights-signal').innerHTML = !S.tuya_configured || anyPhone ? ''
    : ls.someone_in ? `💡 Someone's in – ${esc(ls.why)}`
    : ls.ignored && ls.ignored.length ? `Flat empty · ${esc(ls.ignored.join(', '))} left on (ignored – not touched since you left)`
    : 'Flat empty';
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
    ? `<div class="row" style="margin-top:6px"><span></span><button class="small" onclick="hideDev('${d.id}', true)">Hide</button></div>` : '';
  let state = !d.online ? 'offline' : d.on ? (d.mode === 'colour' ? 'on · colour' : 'on') : 'off';
  if (d.kind === 'dehumidifier') {
    state += d.humidity != null ? ` · ${d.humidity}%` : '';
    if (d.tank_full) state += ' · TANK FULL';
    const extra = `<div class="muted" style="font-size:13px;margin-top:4px">Humidity ${d.humidity ?? '–'}% (set ${d.set_humidity ?? '–'}%) · ${d.temp ?? '–'}° by the unit${d.dmode ? ' · ' + esc(String(d.dmode)) : ''}${d.fan ? ' · fan ' + esc(String(d.fan)) : ''}${d.faults && d.faults.length ? ' · ⚠️ ' + esc(d.faults.join(', ')) : ''}</div>` +
      (d.learned ? `<div class="muted" style="font-size:13px">👕 ${d.learned.loads >= 2 ? `Loads take about ${d.learned.dry_hours} h (learned from ${d.learned.loads})` : `Learning how long loads take (assuming ${d.learned.dry_hours} h for now)`} · dry by ${d.learned.ready_by_text} <span style="font-size:11px">(${esc(d.learned.why)})</span></div>` : '') +
      (full ? `<details style="margin-top:4px"><summary class="muted" style="font-size:12px">Everything it reports</summary><pre style="font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(d.raw, null, 1))}</pre></details>` : '');
    return `<div class="device"><div class="row"><b>💧 ${esc(d.name)}</b><span class="pill ${d.on ? 'on' : ''}">${state}</span></div>${extra}
      <div class="wrap" style="margin-top:8px">${btns}</div>${kidsToggle}</div>`;
  }
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
// Whose phone is this? Asked once, remembered on this phone - so comfort answers count for the right person.
function me() {
  let who = null;
  try { who = localStorage.getItem('me'); } catch (_) {}
  const people = Object.keys(S.presence || {});
  if (!who || !people.includes(who)) {
    who = people.length ? prompt(`Whose phone is this? (${people.join(' or ')})`, people[0]) : null;
    if (who) who = people.find((p) => p.toLowerCase() === who.trim().toLowerCase()) || null;
    try { if (who) localStorage.setItem('me', who); } catch (_) {}
  }
  return who;
}
async function boost(minutes) {
  S.heating = await api('/api/heating/boost', {minutes, person: me()}); render();
  toast(`Boosting to ${S.heating.target_temp}° for ${minutes} min – stops at your comfortable temperature`);
  loadComfort();
}
async function feel(feeling) {
  const person = me();
  if (!person) return;
  await api('/api/comfort', {person, feeling});
  toast({cold: 'Noted – you find this too cold', ok: 'Noted – this feels right', warm: 'Noted – you find this too warm'}[feeling]);
  loadComfort();
}
// ---------- Nest-style smarts ----------
async function loadThermal() {
  let t;
  try { t = await api('/api/thermal'); } catch (_) { return; }
  const line = [];
  if (t.window_pause) line.push('🪟 Window open? Heating paused for 20 min');
  if (t.sun_skew) line.push('☀️ Sun is warming the thermostat');
  if (t.eta_min) line.push(`Warm in about ${t.eta_min} min`);
  const dh = (S.devices || []).find((d) => d.kind === 'dehumidifier' && !d.hidden);
  if (dh && dh.humidity != null) line.push(`💧 ${dh.humidity}% humidity${dh.humidity >= 65 ? ' – damp' : ''}${dh.tank_full ? ' · tank full' : ''}`);
  if ($('thermal-line')) $('thermal-line').textContent = line.join(' · ');
  if (!$('thermal')) return;
  const m = t.model, ex = t.examples, left = Math.max(0, Math.ceil(t.learning_target - t.learning_days));
  let html = m.k ? (ex ? `<div style="padding:3px 0">🔥 At ${ex.outdoor}° outside, warming from 17° to 20° takes ${ex.heat_17_to_20 ?? '…'} min</div>
      <div style="padding:3px 0">🧱 With the heating off it stays within ½° of 20° for about ${ex.hold_half_degree_from_20 ?? '…'} min</div>
      <div style="padding:3px 0">📉 Loses about ${ex.loss_per_hour_at_20}° an hour at 20° when the heating's off</div>` : '') +
      (m.wind_factor >= 1.1 ? `<div style="padding:3px 0">💨 Loses heat ${Math.round((m.wind_factor - 1) * 100)}% faster on windy days</div>` : '') +
      (m.g ? `<div style="padding:3px 0">☀️ Sunshine adds up to ${(m.g * 5).toFixed(1)}°/hour of free heat on a bright day</div>` : '') +
      `<p class="muted" style="font-size:12px">Learned from ${m.cool_runs} cool-down and ${m.heat_runs} warm-up periods${m.confident ? '' : ' – needs 5 of each before it acts on it'}.</p>`
    : '<p>Still measuring how fast the flat warms up and cools down – needs a few heating cycles.</p>';
  const kw = t.kids_wake;
  html += `<div style="padding:6px 0">🧒 Kids usually up: ${kw.weekday || kw.weekend ? `weekdays ${kw.weekday || 'learning'} · weekends ${kw.weekend || 'learning'}` : `learning (${kw.mornings} morning${kw.mornings === 1 ? '' : 's'} so far – from the Fire Sticks, tablets and their lights)`}</div>`;
  html += '<div style="margin-top:8px">' + Object.entries(t.labels).map(([k, label]) => {
    const locked = t.phase3.includes(k) && left > 0;
    return `<label class="row" style="padding:6px 0"><span>${esc(label)}${locked ? `<br><span class="muted" style="font-size:12px">Ready – switches on after the learning phase (${left} day${left === 1 ? '' : 's'} to go)</span>` : ''}</span>
      <input type="checkbox" style="width:auto" ${t.wanted[k] ? 'checked' : ''} onchange="api('/api/thermal/feature',{key:'${k}',on:this.checked}).then(loadThermal)"></label>`;
  }).join('') + '</div>';
  if (t.savings_offset) html += `<p style="margin-top:6px">💷 Savings nudge: aiming ${Math.abs(t.savings_offset)}° below your usual comfort. Tap 🥶 if it's too much and it goes back up.</p>`;
  if (t.suggestions.length) {
    html += '<h3 style="margin:12px 0 4px;font-size:15px">Suggestions</h3>' + t.suggestions.map((s) =>
      `<div style="padding:6px 0;border-top:1px solid var(--line)"><b>Heating down ${esc(s.label)}</b> <span class="muted">· ~£${s.per_year}/yr</span>
       <div class="muted" style="font-size:13px">${esc(s.why)} Only when the flat's actually empty.</div>
       <button class="small" style="margin-top:4px" onclick="api('/api/thermal/suggestion',{days:'${s.days}',start:${s.from},end:${s.to},on:${!s.accepted}}).then(loadThermal)">${s.accepted ? '✓ Accepted – undo' : 'Accept'}</button></div>`).join('');
  }
  $('thermal').innerHTML = html;
}
async function loadSmartReport() {
  let r;
  try { r = await api('/api/thermal/report?days=' + ($('sr-days')?.value || 30)); } catch (_) { return; }
  const c = r.counts, sv = r.saved;
  const line = (icon, label, value, note) => `<div class="row" style="padding:5px 0"><span>${icon} ${label}</span><span><b>${value}</b>${note ? ` <span class="muted" style="font-size:12px">${note}</span>` : ''}</span></div>`;
  const gbp = (v) => v ? `~£${v.toFixed(2)}` : '';
  let html = `<div style="font-size:26px;font-weight:700">${r.saved_total ? '~£' + r.saved_total.toFixed(2) : '£0'}</div>
    <div class="muted" style="font-size:13px;margin-bottom:8px">estimated saved by the smart features in the last ${r.days === 365 ? 'year' : r.days + ' days'}</div>` +
    line('🧱', 'Coasting', `${c.coasting}×`, [c.coasting_min ? c.coasting_min + ' min off early' : '', gbp(sv.coasting)].filter(Boolean).join(' · ')) +
    line('✂️', 'Boosts trimmed', `${c.trimmed}×`, gbp(sv.trimmed)) +
    line('🪟', 'Window pauses', `${c.window}×`, gbp(sv.window)) +
    line('📅', 'Empty-flat turn-downs', `${c.setbacks}×`, gbp(sv.suggestions)) +
    line('💷', 'Savings nudge', r.savings_offset ? `${r.savings_offset}°` : 'off', gbp(sv.nudge)) +
    (c.dehum_cheap_h || c.dehum_deferred ? line('👕', 'Clothes dried on cheap rate', `${(c.dehum_cheap_h || 0).toFixed(1)} h`, gbp(sv.dehumidifier)) : '') +
    line('⏰', 'Early-On (comfort)', `${c.early_on}×`, c.early_on_min ? `warm ${c.early_on_min} min sooner in total` : '') +
    line('☀️', 'Sun on thermostat', `${c.sunblock}×`, 'kept out of learning') +
    line('🥶', 'Comfort taps', `${c.comfort_taps}`, 'what it learns from');
  if (r.learning_left) html += `<p class="muted" style="font-size:13px">Early-On, coasting, nudges and turn-downs switch on in ${r.learning_left} day${r.learning_left === 1 ? '' : 's'} – until then, this mostly shows window pauses, trimmed boosts and learning.</p>`;
  html += r.timeline.length ? '<h3 style="margin:12px 0 4px;font-size:15px">What it did</h3>' + r.timeline.map((t) =>
      `<div style="padding:4px 0;border-top:1px solid var(--line)">${t.icon} ${esc(t.text)} <span class="muted" style="font-size:12px">· ${ago(t.ts)}</span></div>`).join('')
    : '<p class="muted">Nothing yet – actions will appear here as it takes them.</p>';
  html += '<p class="muted" style="font-size:11px;margin-top:6px">£ figures are estimates from your measured boiler use and gas price.</p>';
  $('smart-report').innerHTML = html;
}
async function loadComfort() {
  try {
    const c = await api('/api/comfort');
    const per = Object.entries(c.now.per_person).map(([p, e]) => `${esc(p)} ${e.temp}°${e.confident ? '' : ' (learning)'}`).join(' · ');
    $('comfort-line').innerHTML = `Comfortable this ${esc(c.now.period)}: ${per}` + (c.now.who.length > 1 ? ` · together ${c.now.temp}°` : '');
    if ($('comfort-table')) {
      const periods = ['morning', 'day', 'evening', 'night'];
      $('comfort-table').innerHTML = `<table style="width:100%;font-size:14px;border-collapse:collapse"><tr><td></td>${periods.map((x) => `<td class="muted">${x}</td>`).join('')}</tr>` +
        Object.entries(c.now.per_person).map(([p, e]) => `<tr><td><b>${esc(p)}</b></td>${periods.map((x) => `<td>${e.by_period[x].temp}°<span class="muted" style="font-size:11px">${e.by_period[x].samples ? ' ·' + e.by_period[x].samples : ''}</span></td>`).join('')}</tr>`).join('') +
        '</table><p class="muted" style="font-size:12px">Small numbers = how many times it has learned from you in that part of the day. Tap 🥶/👍/🥵 on Home whenever it feels off (or right).</p>';
    }
  } catch (_) {}
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
  const fmt = (ts) => new Date(ts * 1000).toLocaleString('en-GB',
    hours > 24 * 60 ? {month: 'short', year: 'numeric'} : hours > 24 ? {weekday: 'short', day: 'numeric', month: 'short'} : {hour: '2-digit', minute: '2-digit'});
  grid += `<text x="${P}" y="${H - 2}" fill="#9aa1ad" font-size="10">${fmt(t0)}</text><text x="${W - 4}" y="${H - 2}" fill="#9aa1ad" font-size="10" text-anchor="end">${fmt(t1)}</text>`;
  $('chart').innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${bars}${grid}${line('target_temp', '#777', true)}${line('outdoor_temp', '#4da3ff')}${line('indoor_temp', '#ff8a3d')}</svg>`;
  let on = 0;
  rows.forEach((r, i) => { if (r.boiler_on && rows[i + 1]) on += rows[i + 1].ts - r.ts; });
  $('boiler-hours').textContent = rows.some((r) => r.boiler_on != null)
    ? `Boiler ran for about ${(on / 3600).toFixed(1)} hours (since this app started recording).` : '';
  const im = await api('/api/history/import');
  const when = (ts) => new Date(ts * 1000).toLocaleDateString('en-GB', {month: 'long', year: 'numeric'});
  $('import-status').textContent = im.error && !im.count ? `Couldn't import Hive's past history: ${im.error}`
    : im.count ? `Imported ${im.count.toLocaleString()} past readings from Hive, back to ${when(im.oldest)}${im.done ? '' : ' (still importing…)'}.`
    : 'Importing your past Hive history…';
}

function heatmap(grid, days, color) {
  const cw = 13, ch = 13, L = 30, T = 14, W = L + 24 * cw, H = T + 7 * ch + 2;
  let cells = '';
  grid.forEach((row, d) => row.forEach((v, h) => {
    const fill = v == null ? '#1f232b' : color.replace('A', (0.12 + 0.88 * v).toFixed(2));
    cells += `<rect x="${L + h * cw}" y="${T + d * ch}" width="${cw - 1.5}" height="${ch - 1.5}" rx="2" fill="${fill}"><title>${days[d]} ${String(h).padStart(2, '0')}:00 – ${v == null ? 'no data' : Math.round(v * 100) + '%'}</title></rect>`;
  }));
  let labels = days.map((d, i) => `<text x="0" y="${T + i * ch + 10}" fill="#9aa1ad" font-size="9">${d}</text>`).join('');
  for (const h of [0, 6, 12, 18]) labels += `<text x="${L + h * cw}" y="10" fill="#9aa1ad" font-size="9">${String(h).padStart(2, '0')}</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;display:block;margin:6px 0 4px">${labels}${cells}</svg>`;
}

async function loadRoutine() {
  const r = await api('/api/routine');
  const c = r.collected;
  $('routine-meta').textContent = c.since
    ? `Collecting since ${new Date(c.since * 1000).toLocaleDateString('en-GB', {day: 'numeric', month: 'short'})}: ${c.readings} readings and ${c.presence_events} arrivals/departures so far.`
    : 'No data yet.';
  const block = (title, lines, grid, color) => `<div style="margin:12px 0 4px"><b>${esc(title)}</b></div>` +
    lines.map((l) => `<div class="muted">${esc(l)}</div>`).join('') + heatmap(grid, r.days, color);
  $('routine').innerHTML =
    r.people.map((p) => block(`${p.name} – at home`, p.summary, p.grid, 'rgba(62,207,142,A)')).join('') +
    block('Flat empty (no phones home, no lights on)', r.empty.summary, r.empty.grid, 'rgba(77,163,255,A)') +
    block('Heating on (boiler firing)', r.heating.summary, r.heating.grid, 'rgba(255,138,61,A)');
}

async function loadEvents() {
  const ev = await api('/api/events');
  $('events').innerHTML = ev.length ? ev.map((e) => {
    let d = e.detail; try { const j = JSON.parse(d); d = Object.entries(j).map(([k, v]) => `${k}: ${v}`).join(', '); } catch (_) {}
    return `<div style="padding:4px 0"><span class="muted">${ago(e.ts)}</span> · ${esc(e.kind)} · ${esc(d)}</div>`;
  }).join('') : 'Nothing yet.';
}

// ---------- setup ----------
function whereabouts(name, p) {
  const l = (S.locations || {})[name];
  if (p.home || !l || l.at_home || l.age_min > 120) return '';
  return l.eta_min ? ` <span class="muted">· ${l.miles} mi · home in ~${l.eta_min} min</span>` : ` <span class="muted">· ${l.miles} mi away</span>`;
}
async function loadSetup() {
  const s = await api('/api/setup');
  $('hive-done').style.display = s.hive_configured ? 'block' : 'none';
  $('bright-done').style.display = s.bright_configured ? 'block' : 'none';
  $('cl-done').style.display = s.connectlife_configured ? 'block' : 'none';

  $('cl-form').style.display = s.connectlife_configured ? 'none' : 'block';
  $('bright-form').style.display = s.bright_configured ? 'none' : 'block';
  $('hive-form').style.display = s.hive_configured ? 'none' : 'block';
  if (s.hive_configured) loadZones();
  $('tuya-done').style.display = s.tuya_configured ? 'block' : 'none';
  $('hooks').innerHTML = Object.entries(s.webhooks).map(([p, u]) =>
    `<b>${esc(p)}</b><div class="muted">Arrive</div><div class="url">${esc(u.arrive)}</div><div class="muted">Leave</div><div class="url">${esc(u.leave)}</div>`).join('');
  if ($('owntracks')) $('owntracks').innerHTML = Object.entries(s.owntracks).map(([p, u]) =>
    `<b>${esc(p)}'s iPhone</b><div class="url">${esc(u)}</div>`).join('');
}

async function loadVersion() {
  const v = await api('/api/version');
  const st = v.status && v.status.checked ? ` · last checked ${ago(v.status.checked)} (${v.status.result})` : '';
  $('version').textContent = `Version ${v.commit}${st}`;
  return v;
}

async function copyDiag() {
  const r = await fetch('/api/diagnostics', {credentials: 'same-origin'});
  const text = await r.text();
  const el = $('diag'); el.textContent = text; el.style.display = 'block';
  try { await navigator.clipboard.writeText(text); toast('Copied – paste it into your chat with Claude'); }
  catch (_) { toast('Press and hold the text to copy it'); }
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

// ---------- energy ----------
let energyDays = 30;
const gbp = (v) => '£' + Number(v).toLocaleString('en-GB', {minimumFractionDigits: 0, maximumFractionDigits: 0});
const pct = (v) => Math.round(v * 100) + '%';

async function loadEnergy(days) {
  energyDays = days || energyDays;
  const e = await api('/api/energy?days=' + energyDays);
  $('energy-none').style.display = e.bright_configured ? 'none' : 'block';
  $('energy-ok').style.display = e.bright_configured ? 'block' : 'none';
  if (!e.bright_configured) return;
  const fmtD = (ts) => new Date(ts * 1000).toLocaleDateString('en-GB', {weekday: 'short', day: 'numeric', month: 'short'});
  const behindDays = e.latest ? Math.floor((Date.now() / 1000 - e.latest) / 86400) : null;
  $('energy-meta').innerHTML = (e.error ? 'Last fetch failed: ' + esc(e.error) + '. ' : '') +
    (!e.latest ? 'No meter data yet – Bright can take a day or two to fill in.'
      : behindDays > 2
        ? `⚠️ Bright only has your meter data up to <b>${fmtD(e.latest)}</b> (${behindDays} days ago), so this shows the ${energyDays} days up to then. ` +
          `Bright usually fills gaps in by itself within a few days – if it hasn't by the weekend, email support@glowmarkt.com and ask for a manual data pull.`
        : `Meter data up to ${fmtD(e.latest)}. Smart meter data usually runs about a day behind.`) +
    (e.first ? ` History starts ${fmtD(e.first)}.` : '');
  const el = e.electricity;
  if (el && el.total_kwh < 5) {
    $('e7-verdict').textContent = 'Not enough data yet for a reliable answer.';
    $('e7-detail').textContent = `Only ${el.total_kwh} kWh recorded in this period – check back once Bright has a couple of weeks.`;
    $('baseload').textContent = ''; $('profile-chart').textContent = '';
  } else if (el) {
    const saving = el.per_year_single - el.per_year_e7;
    $('e7-verdict').innerHTML = saving >= 0
      ? `✅ Economy 7 is cheaper for you – about <b>${gbp(saving)}/year</b>`
      : `⚠️ A single-rate tariff would be cheaper – about <b>${gbp(-saving)}/year</b>`;
    $('e7-detail').innerHTML = `${pct(el.night_share)} of your electricity is used in the cheap hours` +
      (el.breakeven_share != null ? ` (you need more than ${pct(el.breakeven_share)} for Economy 7 to pay off at these prices).` : '.') +
      (el.days_covered < 14 ? `<br><b>Only ${el.days_covered} days of data – treat this as a rough guide for now.</b>` : '') +
      `<br>${el.days_covered} days of data: ${el.total_kwh} kWh · Economy 7 ${gbp(el.cost_e7)} vs single rate ${gbp(el.cost_single)} (incl. standing charges).` +
      `<br><span style="font-size:12px">Based on the prices in Setup → Your tariff – check they match your bill.</span>`;
    $('baseload').innerHTML = `<div class="big" style="font-size:36px">${el.baseload_w} W</div>
      <div class="muted">is running all the time (fridge, router, standby…). That's roughly <b>${gbp(el.baseload_cost_year)}/year</b>.
      Under 100 W is good for a flat; anything much higher is worth hunting down with a smart plug.</div>`;
    drawProfile(el.profile, e.tariff);
  } else {
    $('e7-verdict').textContent = 'Waiting for electricity data…'; $('e7-detail').textContent = ''; $('baseload').textContent = '';
    $('profile-chart').textContent = '';
  }
  drawDaily(e.daily);
  const t = e.tariff, c = e.contract || {};
  const cf = c.confirmed || {};
  const label = {fixed: cf.type ? '🔒 Fixed tariff' : '🔒 Looks like a fixed tariff', variable: cf.type ? '📈 Variable tariff' : '📈 Looks like a variable tariff (follows the price cap)', unknown: '❔ Fixed or variable: not known yet'}[c.type] || '';
  const yes = (what, value, text) => `<button class="small" onclick="confirmContract('${what}','${value || ''}')">${text}</button>`;
  const done = (when) => `<span class="ok" style="font-size:13px">✓ confirmed by you</span>`;
  $('prices').innerHTML = `Electricity: day <b>${t.day_rate}p</b> · night <b>${t.night_rate}p</b> · standing <b>${t.e7_standing}p/day</b><br>
    Gas: <b>${t.gas_rate}p</b> · standing <b>${t.gas_standing}p/day</b>
    <div class="wrap" style="margin-top:6px">${cf.prices ? done() : yes('prices', '', '✅ Prices match my bill') + `<button class="small" onclick="showTab('setup')">✏️ Not right</button>`}</div>
    <div class="muted" style="margin-top:6px">${e.tariff_checked ? 'Read from your meter, last checked ' + new Date(e.tariff_checked).toLocaleDateString('en-GB') + '.' : 'Not read from your meter yet.'}</div>
    <div style="margin-top:10px">${label}</div><div class="muted">${esc(c.why || '')}</div>
    <div class="wrap" style="margin-top:6px">${cf.type ? done() : (c.type === 'fixed' ? yes('type', 'fixed', '✅ Yes, it\'s fixed') + yes('type', 'variable', 'No, variable') : c.type === 'variable' ? yes('type', 'variable', '✅ Yes, variable') + yes('type', 'fixed', 'No, fixed') : yes('type', 'fixed', 'It\'s fixed') + yes('type', 'variable', 'It\'s variable'))}</div>` +
    (c.start ? `<div style="margin-top:8px">Started <b>${new Date(c.start).toLocaleDateString('en-GB', {day: 'numeric', month: 'short', year: 'numeric'})}</b>
      <span class="muted">(${c.start_source === 'meter' ? 'from your meter' : c.start_source === 'price change' ? 'when your prices last changed' : 'when this app first saw these prices'})</span></div>` : '') +
    (c.likely_end ? `<div>Likely ends <b>${new Date(c.likely_end).toLocaleDateString('en-GB', {month: 'long', year: 'numeric'})}</b>
      <span class="muted">${c.end_confirmed || cf.end ? '' : "(12-month fix – confirmed automatically the day British Gas changes your prices; you'll get a reminder ~7 weeks before)"}</span></div>
      <div class="wrap" style="margin-top:6px">${cf.end ? done() : yes('end', '', '✅ End date is right') +
        `<button class="small" onclick="$('end-pick').style.display='flex'">Different date</button>`}</div>
      <div id="end-pick" class="wrap" style="display:none;margin-top:6px;align-items:center">
        <input type="date" id="end-date" style="width:auto;margin:0" value="${c.likely_end}">
        <button class="small primary" onclick="saveEndDate()">Save date</button>
        <button class="small" onclick="$('end-pick').style.display='none'">Cancel</button></div>` : '') +
    (cf.prices || cf.type || cf.end ? `<div style="margin-top:8px"><button class="small" onclick="confirmContract('reset')">Undo confirmations</button></div>` : '') +
    ((e.tariff_history || []).length > 1 ? `<div class="muted" style="margin-top:8px">Price changes: ` +
      e.tariff_history.map((h) => `${new Date(h.date).toLocaleDateString('en-GB')}: day ${h.day_rate ?? '–'}p / night ${h.night_rate ?? '–'}p`).join(' · ') + '</div>' : '');
}

function drawDaily(daily) {
  if (!daily.length) { $('daily-chart').textContent = 'No data yet.'; return; }
  const W = 360, H = 170, P = 26, n = daily.length;
  const max = Math.max(...daily.map((d) => Math.max(d.electricity, d.gas)), 1);
  const bw = (W - P) / n;
  let bars = '';
  daily.forEach((d, i) => {
    const x = P + i * bw;
    const he = d.electricity / max * (H - 30), hg = d.gas / max * (H - 30);
    bars += `<rect x="${x}" y="${H - 18 - hg}" width="${Math.max(1, bw * 0.45)}" height="${hg}" fill="#ff8a3d"><title>${d.date} gas ${d.gas} kWh</title></rect>`;
    bars += `<rect x="${x + bw * 0.45}" y="${H - 18 - he}" width="${Math.max(1, bw * 0.45)}" height="${he}" fill="#4da3ff"><title>${d.date} electricity ${d.electricity} kWh</title></rect>`;
  });
  const fmt = (s) => new Date(s).toLocaleDateString('en-GB', {day: 'numeric', month: 'short'});
  const axis = `<text x="2" y="14" fill="#9aa1ad" font-size="10">${Math.round(max)} kWh</text>
    <text x="${P}" y="${H - 2}" fill="#9aa1ad" font-size="10">${fmt(daily[0].date)}</text>
    <text x="${W - 2}" y="${H - 2}" fill="#9aa1ad" font-size="10" text-anchor="end">${fmt(daily[n - 1].date)}</text>`;
  $('daily-chart').innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${axis}${bars}</svg>`;
}

function drawProfile(profile, t) {
  const W = 360, H = 150, P = 26;
  const max = Math.max(...profile, 0.05);
  const bw = (W - P) / 48;
  // Economy 7 window is in GMT; shift to local time for display.
  const off = -new Date().getTimezoneOffset() / 30;  // half-hours ahead of GMT
  const toSlot = (hm) => { const [h, m] = hm.split(':').map(Number); return ((h * 2 + Math.floor(m / 30) + off) % 48 + 48) % 48; };
  const s0 = toSlot(t.night_start), s1 = toSlot(t.night_end);
  let shade = '';
  for (let i = 0; i < 48; i++) {
    const inN = s0 < s1 ? (i >= s0 && i < s1) : (i >= s0 || i < s1);
    if (inN) shade += `<rect x="${P + i * bw}" y="6" width="${bw}" height="${H - 24}" fill="rgba(77,163,255,.12)"/>`;
  }
  const bars = profile.map((v, i) => `<rect x="${P + i * bw + 1}" y="${H - 18 - v / max * (H - 30)}" width="${bw - 2}" height="${v / max * (H - 30)}" fill="#4da3ff"/>`).join('');
  let labels = '';
  for (const h of [0, 6, 12, 18]) labels += `<text x="${P + h * 2 * bw}" y="${H - 2}" fill="#9aa1ad" font-size="10">${String(h).padStart(2, '0')}:00</text>`;
  labels += `<text x="2" y="14" fill="#9aa1ad" font-size="10">${max.toFixed(2)} kWh</text>`;
  $('profile-chart').innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${shade}${bars}${labels}</svg>`;
}

async function syncEnergy() {
  toast('Fetching from Bright…');
  const r = await api('/api/energy/sync', {});
  toast(r.error ? 'Bright: ' + r.error : 'Up to date');
  loadEnergy();
}

async function loadRouter() {
  const r = await api('/api/router');
  $('router-cron').textContent = r.cron;
  $('router-status').innerHTML = r.active
    ? `✅ Router reporting – last update ${ago(r.last_report)}.` + (r.streaming.length ? ` Streaming now: ${esc(r.streaming.join(', '))}.` : '')
    : r.last_report ? `⚠️ No report from the router for ${ago(r.last_report).replace(' ago', '')} – check the scheduled task.` : 'Not set up yet.';
  $('router-setup').style.display = r.active ? 'none' : 'block';
  if (!r.devices.length) { $('router-devices').innerHTML = ''; return; }
  const opts = [['', '—'], ...r.people.map((p) => ['person:' + p, p + "'s phone"]), ['streamer', 'Fire Stick / TV streamer'], ['kids', "Kids' tablet / console"], ['ignore', 'Ignore']];
  $('router-devices').innerHTML = `<p class="muted" style="margin-bottom:4px">Devices on your network (Wi-Fi and wired) – phones and Fire Sticks are recognised by name; fix any that are wrong:</p>` +
    r.devices.slice(0, 40).map((d) => `<div class="device row" style="gap:8px">
      <div style="min-width:0"><b>${esc(d.name || 'Unnamed device')}</b><div class="muted" style="font-size:12px">${d.wired ? '🔌 wired · ' : ''}${esc(d.mac)} · ${d.seen_ago < 120 ? 'online' : 'seen ' + ago(Date.now() / 1000 - d.seen_ago)}${d.kbps > 50 ? ' · ' + d.kbps + ' kbit/s' : ''}</div></div>
      <select onchange="setRouterRole('${d.mac}', this.value)" style="font:inherit;background:#11141a;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px">
        ${opts.map(([v, l]) => `<option value="${v}" ${d.role === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div>`).join('');
}
async function setRouterRole(mac, role) { await api('/api/router/role', {mac, role}); toast('Saved'); }

async function saveEndDate() {
  const v = $('end-date').value;
  if (!v) { toast('Pick a date first'); return; }
  if (new Date(v) < new Date(new Date().toDateString())) { toast('That date is in the past – pick when your fix ends'); return; }
  await confirmContract('end', v);
}

async function confirmContract(what, value) {
  await api('/api/contract/confirm', {what, value: value || ''});
  toast('Thanks – saved'); loadEnergy();
}

// ---------- notifications ----------
function b64ToBytes(b64) {
  const pad = '='.repeat((4 - b64.length % 4) % 4);
  const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}
async function loadPush() {
  const info = await api('/api/push');
  const standalone = window.navigator.standalone || matchMedia('(display-mode: standalone)').matches;
  const supported = 'serviceWorker' in navigator && 'PushManager' in window;
  let subscribed = false;
  if (supported) {
    const reg = await navigator.serviceWorker.ready;
    subscribed = !!(await reg.pushManager.getSubscription());
  }
  $('push-status').innerHTML = !supported || !standalone
    ? 'On iPhone, notifications only work from the home-screen app: in Safari tap Share → <b>Add to Home Screen</b>, open it from the new icon, then come back here.'
    : subscribed ? `✅ Notifications are on for this phone (${info.devices} phone${info.devices === 1 ? '' : 's'} in total).` : 'Tap below and allow notifications.';
  $('push-btn').style.display = supported && standalone && !subscribed ? 'inline-block' : 'none';
  $('push-settings').innerHTML = Object.entries(info.types).map(([k, label]) =>
    `<label class="row" style="padding:6px 0"><span>${esc(label)}</span><input type="checkbox" style="width:auto" ${info.settings[k] ? 'checked' : ''} onchange="api('/api/push/settings',{kind:'${k}',on:this.checked})"></label>`).join('') +
    (subscribed ? `<div class="wrap" style="margin-top:8px"><button class="small" onclick="testPush()">Send a test</button>
      <button class="small" onclick="resetPush()">Reset notifications</button></div><p class="muted" id="push-result" style="font-size:13px"></p>` : '');
  if (info.last && $('push-result')) showPushResult(info.last.results, info.last.ts);
}
function showPushResult(results, ts) {
  const el = $('push-result'); if (!el) return;
  if (!results.length) { el.textContent = 'No phones subscribed – tap Reset, then turn notifications on again.'; return; }
  el.innerHTML = results.map((r, i) => r.ok ? `Phone ${i + 1}: sent ✅ (Apple accepted it)` : `Phone ${i + 1}: failed ❌ ${r.status || ''} ${esc(r.error)}`).join('<br>') +
    (ts ? `<br><span style="font-size:11px">${ago(ts)}</span>` : '');
}
async function testPush() {
  const r = await api('/api/push/test', {});
  showPushResult(r.results);
  toast(r.results.some((x) => x.ok) ? 'Sent – it should arrive in a few seconds' : 'Not delivered – see the message below');
}
async function resetPush() {
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  if (sub) await sub.unsubscribe();
  await api('/api/push/reset', {});
  toast('Reset – now tap Turn on notifications'); loadPush();
}
async function enablePush() {
  try {
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { toast('Notifications were not allowed'); return; }
    const info = await api('/api/push');
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64ToBytes(info.public_key)});
    await api('/api/push/subscribe', sub.toJSON());
    toast('Notifications on'); loadPush();
  } catch (e) { toast('Could not turn on notifications: ' + e.message); }
}

// ---------- learning health ----------
async function loadLearning() {
  const l = await api('/api/learning');
  const pct = Math.min(100, Math.round(l.days / l.target_days * 100));
  $('learning').innerHTML = `<div class="row"><b>Day ${Math.floor(l.days)} of ${l.target_days}</b><span class="muted">${pct}%</span></div>
    <div style="height:8px;background:var(--line);border-radius:4px;margin:6px 0 10px"><div style="height:8px;width:${pct}%;background:var(--warm);border-radius:4px"></div></div>` +
    l.checks.map((c) => `<div style="padding:3px 0">${c.ok ? '✅' : '⚠️'} ${esc(c.name)} <span class="muted" style="font-size:13px">– ${esc(c.detail)}</span></div>`).join('');
}

// ---------- monthly report ----------
async function loadReport() {
  const r = await api('/api/report');
  const row = (label, a, b, fmt) => `<div class="row" style="padding:4px 0"><span class="muted">${label}</span><span>${fmt(a)} <span class="muted" style="font-size:12px">(last month ${fmt(b)})</span></span></div>`;
  const gbp2 = (v) => v ? '£' + v.toFixed(2) : '–';
  const kwh = (v) => v ? v.toFixed(0) + ' kWh' : '–';
  const hrs = (v) => v ? v.toFixed(0) + ' h' : '–';
  const t = r.this, l = r.last;
  $('report').innerHTML = `<div style="margin-bottom:6px"><b>${esc(t.label)}</b> <span class="muted" style="font-size:12px">· ${t.days_of_meter_data} days of meter data</span></div>` +
    row('Electricity', t.electricity_cost, l.electricity_cost, gbp2) + row('', t.electricity_kwh, l.electricity_kwh, kwh) +
    row('Gas', t.gas_cost, l.gas_cost, gbp2) + row('', t.gas_kwh, l.gas_kwh, kwh) +
    row('Boiler ran', t.boiler_hours, l.boiler_hours, hrs) + row('…with nobody home', t.heating_empty_hours, l.heating_empty_hours, hrs) +
    '<p class="muted" style="font-size:12px;margin-top:6px">Costs include standing charges, at your current prices. A full report is sent on the 1st of each month if notifications are on.</p>';
}

async function stStart() {
  $('st-msg').textContent = '';
  try {
    const r = await api('/api/setup/smartthings', {token: $('st-pat').value});
    $('st-link').href = r.url; $('st-approve').style.display = 'block';
  } catch (e) { $('st-msg').textContent = e.message; }
}

async function clLogin() {
  $('cl-msg').textContent = 'Connecting…';
  try {
    const r = await api('/api/setup/connectlife', {username: $('cl-user').value, password: $('cl-pass').value});
    $('cl-msg').textContent = '';
    toast(`ConnectLife connected – ${r.appliances.length} appliance(s) found`);
    loadSetup();
  } catch (e) { $('cl-msg').textContent = e.message; }
}

async function loadOverview() {
  const o = await api('/api/devices/overview');
  const pill = (txt, cls) => `<span class="pill ${cls || ''}">${txt}</span>`;
  const seen = (d) => d.online ? 'connected now' : 'last seen ' + ago(Date.now() / 1000 - d.seen_ago);
  const row = (name, sub, right) => `<div class="device row"><div style="min-width:0"><b>${esc(name)}</b><div class="muted" style="font-size:13px">${sub}</div></div>${right}</div>`;

  // Phones (presence)
  $('phones-card').style.display = o.phones.length || o.people_without_phone.length ? 'block' : 'none';
  $('phones').innerHTML = o.phones.map((p) => row(`${p.person}'s phone`, `${esc(p.name || p.mac)} · ${seen(p)}`,
      p.home ? pill('home', 'home') : pill('out', 'away'))).join('') +
    o.people_without_phone.map((p) => row(`${p}'s phone`, 'Not linked yet – set it in ⚙️ → Router when they\'re home', pill('—'))).join('');

  // Heating
  const h = o.heating;
  $('heat-dev-card').style.display = h && h.connected ? 'block' : 'none';
  if (h && h.connected) $('heat-dev').innerHTML = row(`Hive thermostat${h.zone_name ? ' · ' + esc(h.zone_name) : ''}`,
    `${h.indoor_temp ?? '–'}° now · target ${h.target_temp ?? '–'}° · ${esc(h.mode || '')}${h.online === false ? ' · OFFLINE' : ''}`,
    h.boiler_on ? pill('🔥 heating', 'on') : pill('idle'));

  // Fire Sticks
  $('tv-card').style.display = o.streamers.length ? 'block' : 'none';
  $('tv-devs').innerHTML = o.streamers.map((d) => row(d.name || 'Fire Stick', `${seen(d)}${d.kbps > 50 ? ' · ' + (d.kbps / 1000).toFixed(1) + ' Mbit/s' : ''}`,
    d.streaming ? pill('▶ streaming', 'on') : d.online ? pill('idle') : pill('off', 'away'))).join('');

  // Kids' devices
  $('kids-dev-card').style.display = o.kids.length ? 'block' : 'none';
  $('kids-devs').innerHTML = o.kids.map((d) => row(d.name || "Kids' device", seen(d),
    d.in_use ? pill('in use', 'on') : d.online ? pill('idle') : pill('off', 'away'))).join('');

  // Everything else on the Wi-Fi
  $('other-card').style.display = o.other_wifi.length ? 'block' : 'none';
  $('other-devs').innerHTML = `<details><summary class="muted">${o.other_wifi.length} device${o.other_wifi.length === 1 ? '' : 's'}</summary>` +
    o.other_wifi.map((d) => row(d.name || 'Unnamed device', `${esc(d.mac)} · ${seen(d)}`, d.role === 'ignore' ? pill('ignored') : pill(d.online ? 'online' : 'offline'))).join('') + '</details>';
}

async function loadAppliances(refresh) {
  const r = await api('/api/appliances' + (refresh ? '?refresh=true' : ''));
  $('appl-card').style.display = r.configured ? 'block' : 'none';
  if (!r.configured) return;
  $('appliances').innerHTML = (r.error ? `<p class="err">${esc(r.error)}</p>` : '') +
    (r.appliances.length ? r.appliances.map((a) => {
      const fields = Object.entries(a.status || {});
      const changing = (a.changing || []).map((k) => `${esc(k)} = ${esc(a.status[k])}`).join('<br>');
      const w = a.wash || {};
      const pill = w.running ? `${w.drying ? 'drying' : 'washing'}${w.remaining_min != null ? ' · ' + w.remaining_min + ' min left' : ''}` : (a.offline ? 'off' : 'idle');
      const fin = w.running && w.finish_ts ? `<div style="margin-top:4px">Finishes about <b>${new Date(w.finish_ts * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}</b></div>` : '';
      const sm = r.summary && r.summary.runs ? `<div class="muted" style="font-size:13px;margin-top:4px">Last 30 days: ${r.summary.runs} washes (${r.summary.dried} with drying) · ${r.summary.day_runs} on the day rate</div>` : '';
      return `<div class="device"><div class="row"><b>🧺 ${esc(a.name)}</b><span class="pill ${w.running ? 'on' : ''}">${pill}</span></div>${fin}${sm}
        <div class="muted">${esc(a.type || '')} · ${fields.length} readings · updated ${ago(a.updated)}</div>
        ${changing ? `<details style="margin-top:6px"><summary class="muted">Changed recently</summary><div class="muted">${changing}</div></details>` : ''}
        <details style="margin-top:6px"><summary class="muted">Everything it reports</summary><div class="url">${fields.map(([k, v]) => `${esc(k)}: ${esc(v)}`).join('<br>')}</div></details></div>`;
    }).join('') : '<p class="muted">No appliances found on this ConnectLife account.</p>') +
    `<button class="small" style="margin-top:8px" onclick="loadAppliances(true)">↻ Refresh</button>`;
}

async function brightLogin() {
  $('bright-msg').textContent = '';
  try {
    await api('/api/setup/bright', {username: $('bright-user').value, password: $('bright-pass').value});
    toast('Bright connected – downloading your history in the background');
    loadSetup();
  } catch (e) { $('bright-msg').textContent = e.message; }
}

const TARIFF_FIELDS = [
  ['day_rate', 'Economy 7 day rate (p/kWh)'], ['night_rate', 'Economy 7 night rate (p/kWh)'],
  ['e7_standing', 'Economy 7 standing charge (p/day)'], ['night_start', 'Cheap hours start (GMT, e.g. 00:30)'],
  ['night_end', 'Cheap hours end (GMT, e.g. 07:30)'], ['single_rate', 'Single-rate tariff to compare (p/kWh)'],
  ['single_standing', 'Single-rate standing charge (p/day)'], ['gas_rate', 'Gas rate (p/kWh)'], ['gas_standing', 'Gas standing charge (p/day)'],
];
async function loadTariffForm() {
  const e = await api('/api/energy?days=1');
  $('tariff-form').innerHTML = TARIFF_FIELDS.map(([k, label]) =>
    `<label class="muted" style="font-size:13px">${label}</label><input id="t-${k}" value="${esc(e.tariff[k])}" ${k.includes('night_s') || k.includes('night_e') ? '' : 'inputmode="decimal"'}>`).join('');
}
async function tariffFromMeter() {
  $('meter-tariff-msg').textContent = 'Reading your meter’s prices from Bright…';
  try {
    const r = await api('/api/tariff/from-meter');
    const v = r.values, keys = Object.keys(v);
    if (!keys.length) {
      $('meter-tariff-msg').innerHTML = 'Your meter didn’t report any prices yet. Fill them in from your bill for now.' +
        `<details><summary>Details</summary><div class="url">${esc(JSON.stringify(r.raw).slice(0, 1500))}</div></details>`;
      return;
    }
    for (const k of keys) { const el = $('t-' + k); if (el) el.value = v[k]; }
    const e7 = v.day_rate != null;
    $('meter-tariff-msg').innerHTML = (e7
      ? `Found Economy 7 prices: day <b>${v.day_rate}p</b>, night <b>${v.night_rate}p</b>`
      : v.single_rate != null ? `Found one electricity price: <b>${v.single_rate}p</b> (your meter may not report the day/night split – check your bill)` : 'No electricity prices found')
      + (v.e7_standing != null ? `, standing charge <b>${v.e7_standing}p/day</b>` : '')
      + (v.gas_rate != null ? `. Gas <b>${v.gas_rate}p</b>` + (v.gas_standing != null ? `, standing <b>${v.gas_standing}p/day</b>` : '') : '')
      + `.<br>Check these against your bill, add your cheap-hours times and a single-rate price to compare, then tap <b>Save tariff</b>.` +
      `<details><summary>What the meter sent</summary><div class="url">${esc(JSON.stringify(r.raw).slice(0, 1500))}</div></details>`;
  } catch (e) { $('meter-tariff-msg').textContent = e.message; }
}

async function saveTariff() {
  const body = {};
  for (const [k] of TARIFF_FIELDS) body[k] = k.startsWith('night_') ? $('t-' + k).value.trim() : parseFloat($('t-' + k).value);
  await api('/api/tariff', body); toast('Tariff saved');
}

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
{
  const q = new URLSearchParams(location.search).get('smartthings');
  if (q) {
    history.replaceState(null, '', '/');
    setTimeout(() => toast({ok: 'SmartThings connected – your TV is on the Devices tab', failed: 'SmartThings approval failed – try the setup again', cancelled: 'SmartThings approval cancelled'}[q] || ''), 500);
  }
}
load();
setInterval(() => { if (!document.hidden && pendingTarget === null) load(); }, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
