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
  if (tab === 'summary') loadSummary();
  if (b.dataset.tab === 'history') { loadRoutine(); loadHistory(24); loadEvents(); }
  if (b.dataset.tab === 'energy') loadEnergy(30);
  if (tab === 'devices') loadAppliances(false);
  if (b.dataset.tab === 'setup') { loadSetup(); loadVersion(); loadTariffForm(); }
}

// ---------- home ----------
async function load() {
  S = await api('/api/status');
  render();
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
  if (S.weather) $('outdoor').textContent = `Outside ${S.weather.temp.toFixed(1)}°`;

  // Home only shows problems that affect it right now (heating / lights). Others appear on the Summary tab.
  const errs = Object.entries(S.errors || {}).filter(([k]) => k === 'hive' || k === 'tuya');
  $('errors').innerHTML = errs.length ? `<div class="card err">${errs.map(([k, v]) => `<b>${esc(k)}</b>: ${esc(v)}`).join('<br>')}</div>` : '';

  $('people').innerHTML = Object.entries(S.presence).map(([name, p]) =>
    `<button class="pill ${p.home ? 'home' : 'away'}" onclick="togglePerson('${esc(name)}', ${!p.home})">${esc(name)} · ${p.home ? 'home' : 'out'} <span class="muted">${ago(p.since)}</span></button>`).join('');
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
async function loadSetup() {
  const s = await api('/api/setup');
  $('hive-done').style.display = s.hive_configured ? 'block' : 'none';
  $('bright-done').style.display = s.bright_configured ? 'block' : 'none';
  $('cl-done').style.display = s.connectlife_configured ? 'block' : 'none';
  $('st-done').style.display = s.smartthings_configured ? 'block' : 'none';
  $('st-form').style.display = s.smartthings_configured ? 'none' : 'block';
  $('cl-form').style.display = s.connectlife_configured ? 'none' : 'block';
  $('bright-form').style.display = s.bright_configured ? 'none' : 'block';
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
  const label = {fixed: '🔒 Looks like a fixed tariff', variable: '📈 Looks like a variable tariff (follows the price cap)', unknown: '❔ Fixed or variable: not known yet'}[c.type] || '';
  $('prices').innerHTML = `Electricity: day <b>${t.day_rate}p</b> · night <b>${t.night_rate}p</b> · standing <b>${t.e7_standing}p/day</b><br>
    Gas: <b>${t.gas_rate}p</b> · standing <b>${t.gas_standing}p/day</b>
    <div class="muted" style="margin-top:6px">${e.tariff_checked ? 'Read from your meter, last checked ' + new Date(e.tariff_checked).toLocaleDateString('en-GB') + '.' : 'Not read from your meter yet.'}</div>
    <div style="margin-top:10px">${label}</div><div class="muted">${esc(c.why || '')}</div>` +
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

async function loadAppliances(refresh) {
  const r = await api('/api/appliances' + (refresh ? '?refresh=true' : ''));
  $('appl-card').style.display = r.configured ? 'block' : 'none';
  if (!r.configured) return;
  $('appliances').innerHTML = (r.error ? `<p class="err">${esc(r.error)}</p>` : '') +
    (r.appliances.length ? r.appliances.map((a) => {
      const fields = Object.entries(a.status || {});
      const changing = (a.changing || []).map((k) => `${esc(k)} = ${esc(a.status[k])}`).join('<br>');
      return `<div class="device"><div class="row"><b>${esc(a.name)}</b><span class="pill">${a.offline ? 'offline' : 'online'}</span></div>
        <div class="muted">${esc(a.type || '')} · ${fields.length} readings · updated ${ago(a.updated)}</div>
        ${changing ? `<div class="muted" style="margin-top:6px"><b>Changed recently:</b><br>${changing}</div>` : '<div class="muted" style="margin-top:6px">Run a wash and the fields that change will show here.</div>'}
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
