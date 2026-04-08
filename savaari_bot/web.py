"""FastAPI dashboard + first-run wizard.

The dashboard is intentionally tiny: a single HTML page that polls /api/status
every 3 seconds via fetch(). No build step, no JS framework, no static-asset
dance to fight with PyInstaller. Everything is inlined.

Routes:
  GET  /                  -> the HTML page (serves wizard if no token, else dash)
  GET  /api/status        -> JSON snapshot of AppState
  POST /api/config        -> save vendor_token / telegram creds / poll interval
  POST /api/pause         -> toggle paused flag
  POST /api/quit          -> request shutdown (used by Quit button)
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import config, db, fleet
from .state import AppState

log = logging.getLogger("savaari_bot.web")


INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Savaari Bot</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 720px;
         margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
  h1 { font-size: 1.4rem; margin-bottom: 0.2rem; }
  .sub { color: #888; margin-top: 0; }
  .card { border: 1px solid #4443; border-radius: 8px; padding: 1rem 1.2rem;
          margin: 1rem 0; }
  .ok    { color: #2a8a2a; }
  .warn  { color: #c47b00; }
  .err   { color: #c0392b; }
  label { display: block; margin: 0.6rem 0 0.2rem; font-size: 0.9rem; }
  input[type=text], input[type=number], textarea {
    width: 100%; padding: 0.5rem; box-sizing: border-box;
    border: 1px solid #8884; border-radius: 4px; font-family: inherit;
  }
  button { padding: 0.5rem 1rem; border-radius: 4px; border: 1px solid #4448;
           background: #fff2; cursor: pointer; }
  button.primary { background: #2a6df4; color: white; border-color: #2a6df4; }
  button.danger  { background: #c0392b; color: white; border-color: #c0392b; }
  .row { display: flex; gap: 1rem; flex-wrap: wrap; }
  .row > div { flex: 1; min-width: 180px; }
  code { background: #8881; padding: 0.1em 0.4em; border-radius: 3px; }
  .hidden { display: none; }
</style>
</head><body>

<h1>Savaari Bot</h1>
<p class="sub" id="subtitle">connecting…</p>

<div id="wizard" class="card hidden">
  <h2 style="margin-top:0">First-run setup</h2>
  <p>Paste the values below. The bot starts polling as soon as the
  <code>vendorToken</code> is set; Telegram is optional and used by
  Phase&nbsp;1+ alerts.</p>
  <form id="wizard-form">
    <label>Savaari vendorToken
      <textarea name="vendor_token" rows="3" required
        placeholder="On vendor.savaari.com, open DevTools console and run:  sessionStorage.vendorToken"></textarea>
    </label>
    <div class="row">
      <div>
        <label>Telegram bot token (optional)
          <input type="text" name="telegram_bot_token" placeholder="123:ABC...">
        </label>
      </div>
      <div>
        <label>Telegram chat ID (optional)
          <input type="text" name="telegram_chat_id" placeholder="e.g. 123456789">
        </label>
      </div>
    </div>
    <div class="row">
      <div>
        <label>Poll interval (seconds)
          <input type="number" name="poll_interval_s" value="10" min="3" max="120">
        </label>
      </div>
      <div>
        <label>Fare floor (₹, ignore alerts below)
          <input type="number" name="fare_floor" value="0" min="0">
        </label>
      </div>
    </div>
    <div class="row">
      <div>
        <label>Floor applies to
          <select name="fare_floor_basis">
            <option value="net" selected>Net (after fuel/driver/toll)</option>
            <option value="gross">Gross (Savaari fare)</option>
          </select>
        </label>
      </div>
    </div>
    <p style="margin-top:0.6rem">
      <label style="display:flex;align-items:center;gap:0.5rem;font-size:0.9rem">
        <input type="checkbox" name="dry_run_accept" checked>
        Dry-run accepts (Confirm taps are logged but no booking is taken).
        Leave on until you've tested with a real alert.
      </label>
    </p>
    <p style="margin-top:1rem">
      <button class="primary" type="submit">Save and start</button>
    </p>
  </form>
</div>

<div id="dash" class="card hidden">
  <div class="row">
    <div>
      <strong>Status:</strong> <span id="status-pill">—</span><br>
      <small>Started: <span id="started">—</span></small>
    </div>
    <div>
      <strong>Last poll:</strong> <span id="last-ok">—</span><br>
      <small><span id="last-stats">—</span></small>
    </div>
  </div>
  <p id="error-line" class="err hidden"></p>
  <p><strong>Today:</strong>
     <span id="alerts-today">0</span> alerts ·
     <span id="confirms-today">0</span> confirms
     · <span id="dry-run-pill"></span>
  </p>
  <p style="margin-top:1rem">
    <button id="pause-btn">Pause</button>
    <button id="test-btn">Send test alert</button>
    <button id="settings-btn">Settings</button>
    <button id="profit-btn">Profit settings</button>
    <button id="gate-btn">Eligibility gate</button>
    <button id="fleet-btn">Fleet &amp; deadhead</button>
    <button id="analytics-btn">Analytics</button>
    <button id="quit-btn" class="danger">Quit</button>
  </p>
</div>

<div id="analytics" class="card hidden">
  <h2 style="margin-top:0">Route analytics</h2>
  <p>Aggregated from all the broadcast history the bot has collected.
  A "taken" broadcast is one where another vendor responded before it
  vanished; "cancelled" is one that vanished without any responders.</p>
  <div class="row">
    <div>
      <label>Window (days)
        <input type="number" id="ana-days" value="14" min="1" max="60">
      </label>
    </div>
    <div>
      <label>&nbsp;
        <button id="ana-refresh">Refresh</button>
      </label>
    </div>
  </div>
  <div id="ana-headline" style="margin-top:0.6rem"></div>
  <table id="ana-table" style="width:100%;border-collapse:collapse;font-size:0.85rem;margin-top:0.6rem">
    <thead>
      <tr style="text-align:left;border-bottom:1px solid #8884">
        <th>Route</th><th>Car</th><th style="width:60px">n</th>
        <th style="width:80px">Avg resp</th><th style="width:60px">Take</th>
        <th style="width:80px">Avg fare</th><th style="width:80px">Δ fare</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
  <h3 style="font-size:1rem;margin-top:1.2rem">Escalation curves</h3>
  <p style="font-size:0.85rem;color:#888">
    Per route: median price-step count and the P50/P90 final fare. The
    notifier uses these to tag each alert as <b>WAIT</b>, <b>GRAB</b> or
    <b>OK</b>. "Wait" means the current price is &gt;10% below P50 on a
    high-take-rate route — likely to escalate before someone takes it.
  </p>
  <form id="esc-form" style="margin-top:0.4rem">
    <label style="display:flex;align-items:center;gap:0.5rem">
      <input type="checkbox" name="annotate_escalation" checked>
      Annotate escalation hint in every alert
    </label>
    <label style="display:flex;align-items:center;gap:0.5rem;margin-top:0.4rem">
      <input type="checkbox" name="suppress_below_p50">
      <strong>Suppress alerts when model says wait</strong>
      (skip this until you have ~2 weeks of history; can lose real opportunities)
    </label>
    <p style="margin-top:0.4rem">
      <button class="primary" type="submit">Save escalation settings</button>
    </p>
  </form>
  <table id="esc-table" style="width:100%;border-collapse:collapse;font-size:0.85rem;margin-top:0.4rem">
    <thead>
      <tr style="text-align:left;border-bottom:1px solid #8884">
        <th>Route</th><th>Car</th><th>n</th>
        <th>Med steps</th><th>P10</th><th>P50</th><th>P90</th><th>Take</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>

  <h3 style="font-size:1rem;margin-top:1.2rem">Weekly report</h3>
  <p>
    <button id="ana-preview-btn">Preview report</button>
    <button id="ana-send-btn" class="primary">Send to Telegram now</button>
  </p>
  <pre id="ana-preview" style="background:#8881;padding:0.6rem;border-radius:4px;font-size:0.85rem;display:none;white-space:pre-wrap"></pre>
</div>

<div id="fleet" class="card hidden">
  <h2 style="margin-top:0">Fleet &amp; deadhead</h2>
  <p>Add the cars you actually run. Each car's location is geocoded once
  via Nominatim; the bot then routes from there to every booking pickup
  via OSRM and picks the closest one. Deadhead fuel + driver cost gets
  subtracted from the net profit shown in alerts.</p>
  <form id="fleet-toggle-form">
    <label style="display:flex;align-items:center;gap:0.5rem">
      <input type="checkbox" name="enable_deadhead">
      <strong>Enable deadhead-aware alerts</strong>
      (off until you've added at least one car with a known location)
    </label>
    <p style="margin-top:0.6rem">
      <label style="display:block;font-size:0.85rem;color:#888">Nominatim User-Agent (set this to a real contact!)</label>
      <input type="text" name="nominatim_user_agent" placeholder="savaari_bot (you@example.com)">
    </p>
    <p>
      <button class="primary" type="submit">Save</button>
    </p>
  </form>

  <h3 style="font-size:1rem;margin-top:1.2rem">Cars</h3>
  <table id="fleet-table" style="width:100%;border-collapse:collapse;font-size:0.9rem">
    <thead>
      <tr style="text-align:left;border-bottom:1px solid #8884">
        <th>Label</th>
        <th>Car type</th>
        <th>Location</th>
        <th>Coords</th>
        <th></th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>

  <h3 style="font-size:1rem;margin-top:1.2rem">Sync from Savaari</h3>
  <p style="font-size:0.85rem;color:#888;margin:0 0 0.4rem">
    Pulls your registered fleet directly from Savaari (no token needed
    beyond the vendor_id we already auto-discovered). Existing cars
    matched by their Savaari id get refreshed; new ones are inserted
    with empty location for you to fill in. Manually-added cars are
    never touched. Drivers are also cached for future use.
  </p>
  <p>
    <button id="fleet-sync-btn" type="button">Sync now</button>
    <span id="fleet-sync-result" style="font-size:0.85rem;color:#888;margin-left:0.5rem"></span>
  </p>

  <h3 style="font-size:1rem;margin-top:1.2rem">Add a car manually</h3>
  <form id="fleet-add-form">
    <div class="row">
      <div>
        <label>Label
          <input type="text" name="label" required placeholder="KA-05-MX-1234 / Ramesh">
        </label>
      </div>
      <div>
        <label>Car type
          <select name="car_type_id">
            <option value="">Any (no filter)</option>
          </select>
        </label>
      </div>
    </div>
    <label>Location text (address, area, landmark)
      <input type="text" name="location_text" required placeholder="Khetwadi, Mumbai">
    </label>
    <p>
      <button class="primary" type="submit">Add car (geocodes immediately)</button>
    </p>
  </form>
  <pre id="fleet-msg" style="background:#8881;padding:0.6rem;border-radius:4px;font-size:0.85rem;display:none"></pre>
</div>

<div id="gate" class="card hidden">
  <h2 style="margin-top:0">Driver/car availability gate</h2>
  <p>Before alerting, the bot can call Savaari's
  <code>FETCH_DRIVERS_WITH_CARS_LIST_NPS</code> for the booking and check
  whether you have any (driver, car) combinations that can serve it. When
  enabled, alerts where the count is zero are silently dropped.</p>
  <p><strong>vendor_user_id:</strong> <code id="gate-uid">—</code></p>
  <form id="gate-form">
    <label style="display:flex;align-items:center;gap:0.5rem">
      <input type="checkbox" name="annotate_eligibility">
      Show eligibility count in every alert (annotate-only mode)
    </label>
    <label style="display:flex;align-items:center;gap:0.5rem;margin-top:0.4rem">
      <input type="checkbox" name="require_eligible_car">
      <strong>Suppress alerts when no eligible cars</strong>
      (don't tick until you've tested with the button below — your fleet
      may legitimately have zero eligible cars right now)
    </label>
    <div class="row" style="margin-top:0.6rem">
      <div>
        <label>Cache TTL (seconds)
          <input type="number" name="eligibility_cache_ttl_s" value="60" min="5" max="600">
        </label>
      </div>
    </div>
    <p style="margin-top:1rem">
      <button class="primary" type="submit">Save gate settings</button>
      <button id="gate-test-btn" type="button">Test gate (uses a real booking)</button>
    </p>
  </form>
  <pre id="gate-test-result" style="background:#8881;padding:0.6rem;border-radius:4px;font-size:0.85rem;overflow:auto;max-height:240px"></pre>
</div>

<div id="profit" class="card hidden">
  <h2 style="margin-top:0">Profit estimator</h2>
  <p>The bot subtracts these per-km costs from the vendor fare to compute
  a net profit, which it shows in every alert. Tune them to match your
  fleet's actual fuel + driver pay structure.</p>
  <form id="profit-form">
    <div class="row">
      <div>
        <label>Fuel default ₹/km
          <input type="number" step="0.1" name="fuel_rate_default" value="8.5">
        </label>
      </div>
      <div>
        <label>Driver default % of booking
          <input type="number" step="0.5" min="0" max="100" name="driver_pct_default" value="25">
        </label>
      </div>
    </div>
    <p style="font-size:0.85rem;color:#888;margin:0.2rem 0">
      Driver pay is computed as a percentage of <code>vendor_cost</code>
      (plus night charge when applicable). Tolls are not modelled — if
      you want a rough toll allowance, raise the fuel ₹/km slightly.
    </p>
    <h3 style="font-size:1rem;margin:1rem 0 0.4rem">Per car type overrides</h3>
    <p style="margin-top:0;font-size:0.85rem;color:#888">
      Leave blank to use the defaults above. Car list is populated from the
      latest poll.
    </p>
    <table id="cars-table" style="width:100%;border-collapse:collapse;font-size:0.9rem">
      <thead>
        <tr style="text-align:left;border-bottom:1px solid #8884">
          <th>Car</th>
          <th style="width:100px">Fuel ₹/km</th>
          <th style="width:100px">Driver %</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
    <p style="margin-top:1rem">
      <button class="primary" type="submit">Save profit settings</button>
    </p>
  </form>
</div>

<script>
async function refresh() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('subtitle').textContent =
      s.config.vendor_token_set ? 'running locally on :8765' : 'first-run setup';

    if (!s.config.vendor_token_set) {
      // First-run mode: force the wizard open and hide the dashboard.
      document.getElementById('wizard').classList.remove('hidden');
      document.getElementById('dash').classList.add('hidden');
      return;
    }
    // Token is set: always show the dash, but DON'T touch the wizard.
    // The user toggles it via the Settings button — if we forced it
    // closed here, the 3-second refresh loop would slam it shut while
    // they're trying to use it.
    document.getElementById('dash').classList.remove('hidden');

    const pill = document.getElementById('status-pill');
    if (s.auth_failed) { pill.textContent = '⚠ token rejected'; pill.className = 'err'; }
    else if (s.paused) { pill.textContent = '⏸ paused';        pill.className = 'warn'; }
    else if (s.last_ok_at) { pill.textContent = '● running';   pill.className = 'ok'; }
    else { pill.textContent = '… starting';                    pill.className = ''; }

    document.getElementById('started').textContent = s.started_at || '—';
    document.getElementById('last-ok').textContent = s.last_ok_at || '—';
    const lp = s.last_poll;
    document.getElementById('last-stats').textContent =
      `${lp.total_broadcasts} broadcasts · ${lp.new_count} new · ${lp.price_up_count} price-up · ${lp.vanished_count} vanished`;

    const errLine = document.getElementById('error-line');
    if (s.last_error_msg) {
      errLine.textContent = `last error @ ${s.last_error_at}: ${s.last_error_msg}`;
      errLine.classList.remove('hidden');
    } else {
      errLine.classList.add('hidden');
    }

    document.getElementById('pause-btn').textContent = s.paused ? 'Resume' : 'Pause';
    document.getElementById('alerts-today').textContent   = s.today.alerts_today;
    document.getElementById('confirms-today').textContent = s.today.confirms_today;
    const dr = document.getElementById('dry-run-pill');
    if (s.config.dry_run_accept) { dr.textContent = '⚠ DRY-RUN'; dr.className = 'warn'; }
    else { dr.textContent = 'LIVE'; dr.className = 'ok'; }
  } catch (e) {
    document.getElementById('subtitle').textContent = 'connection lost';
  }
}

document.getElementById('wizard-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const body = Object.fromEntries(fd.entries());
  // Checkboxes don't appear in FormData when unchecked.
  body.dry_run_accept = ev.target.dry_run_accept.checked;
  const btn = ev.target.querySelector('button[type=submit]');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    const r = await fetch('/api/config', {
      method: 'POST', headers: {'content-type':'application/json'},
      body: JSON.stringify(body),
    });
    if (r.ok) {
      // Explicitly close the wizard — refresh() no longer does it for us,
      // and otherwise the user has no visual confirmation that the save
      // succeeded.
      document.getElementById('wizard').classList.add('hidden');
      btn.textContent = '✓ Saved';
      refresh();
    } else {
      alert('Save failed: ' + (await r.text()));
    }
  } catch (e) {
    alert('Save failed: ' + e);
  } finally {
    setTimeout(() => { btn.disabled = false; btn.textContent = orig; }, 1500);
  }
});

document.getElementById('test-btn').addEventListener('click', async () => {
  const r = await fetch('/api/test-alert', {method: 'POST'});
  const j = await r.json();
  alert(j.detail || j.result || 'sent');
});

async function loadProfitForm() {
  const [carsR, profitR] = await Promise.all([
    fetch('/api/cars'),
    fetch('/api/profit-config'),
  ]);
  const cars = (await carsR.json()).cars || [];
  const profit = await profitR.json();
  const form = document.getElementById('profit-form');
  form.fuel_rate_default.value  = profit.fuel_rate_default;
  form.driver_pct_default.value = profit.driver_pct_default;

  const tbody = document.querySelector('#cars-table tbody');
  tbody.innerHTML = '';
  for (const c of cars) {
    const tr = document.createElement('tr');
    tr.style.borderBottom = '1px solid #8882';
    const fuel = profit.fuel_rate_per_car_type[c.car_type_id] ?? '';
    const pct  = profit.driver_pct_per_car_type[c.car_type_id] ?? '';
    tr.innerHTML =
      `<td style="padding:0.3rem 0">${c.car_name} <small style="color:#888">(${c.car_type_id})</small></td>` +
      `<td><input type="number" step="0.1" name="fuel_${c.car_type_id}" value="${fuel}" placeholder="default"></td>` +
      `<td><input type="number" step="0.5" min="0" max="100" name="driver_${c.car_type_id}" value="${pct}" placeholder="default"></td>`;
    tbody.appendChild(tr);
  }
  if (cars.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3" style="padding:0.6rem;color:#888">No car types known yet — wait for the first poll.</td></tr>';
  }
}

document.getElementById('profit-btn').addEventListener('click', () => {
  const p = document.getElementById('profit');
  if (p.classList.contains('hidden')) {
    loadProfitForm().then(() => p.classList.remove('hidden'));
  } else {
    p.classList.add('hidden');
  }
});

async function loadGateForm() {
  const r = await fetch('/api/gate-config');
  const g = await r.json();
  document.getElementById('gate-uid').textContent = g.vendor_user_id || '— (will populate after first poll)';
  const f = document.getElementById('gate-form');
  f.annotate_eligibility.checked   = g.annotate_eligibility;
  f.require_eligible_car.checked   = g.require_eligible_car;
  f.eligibility_cache_ttl_s.value  = g.eligibility_cache_ttl_s;
  document.getElementById('gate-test-result').textContent = '';
}

document.getElementById('gate-btn').addEventListener('click', () => {
  const g = document.getElementById('gate');
  if (g.classList.contains('hidden')) {
    loadGateForm().then(() => g.classList.remove('hidden'));
  } else {
    g.classList.add('hidden');
  }
});

document.getElementById('gate-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const body = {
    annotate_eligibility:    ev.target.annotate_eligibility.checked,
    require_eligible_car:    ev.target.require_eligible_car.checked,
    eligibility_cache_ttl_s: parseFloat(fd.get('eligibility_cache_ttl_s')),
  };
  const r = await fetch('/api/gate-config', {
    method: 'POST', headers: {'content-type':'application/json'},
    body: JSON.stringify(body),
  });
  if (r.ok) alert('Saved.');
  else alert('Save failed: ' + (await r.text()));
});

document.getElementById('gate-test-btn').addEventListener('click', async () => {
  const out = document.getElementById('gate-test-result');
  out.textContent = 'testing…';
  const r = await fetch('/api/test-availability', {method: 'POST'});
  const j = await r.json();
  out.textContent = JSON.stringify(j, null, 2);
});

async function loadAnalytics() {
  const days = parseInt(document.getElementById('ana-days').value || '14', 10);
  const [r, er, cr] = await Promise.all([
    fetch('/api/analytics?days=' + days),
    fetch('/api/escalation?days=' + days),
    fetch('/api/escalation-config'),
  ]);
  const j = await r.json();
  const ej = await er.json();
  const ec = await cr.json();
  const h = j.headline || {};
  document.getElementById('ana-headline').innerHTML =
    `<strong>Last ${j.days} days:</strong> ` +
    `${h.total||0} broadcasts · ${h.taken||0} taken · ${h.cancelled||0} cancelled · ` +
    `${h.alerts_fired||0} alerts · ${h.confirms||0} confirms`;
  const tbody = document.querySelector('#ana-table tbody');
  tbody.innerHTML = '';
  const rows = j.routes || [];
  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="padding:0.6rem;color:#888">No route history yet.</td></tr>';
  }
  for (const s of rows) {
    const tr = document.createElement('tr');
    tr.style.borderBottom = '1px solid #8882';
    tr.innerHTML =
      `<td style="padding:0.3rem 0">${s.source_name} → ${s.dest_name}</td>` +
      `<td>${s.car_name}</td>` +
      `<td>${s.samples}</td>` +
      `<td>${s.avg_responders.toFixed(1)}</td>` +
      `<td>${Math.round(s.take_rate*100)}%</td>` +
      `<td>₹${(s.avg_first_fare||0).toLocaleString()}</td>` +
      `<td>+₹${(s.avg_escalation||0).toLocaleString()}</td>`;
    tbody.appendChild(tr);
  }

  // Escalation table.
  const etbody = document.querySelector('#esc-table tbody');
  etbody.innerHTML = '';
  const erows = ej.routes || [];
  if (erows.length === 0) {
    etbody.innerHTML = '<tr><td colspan="8" style="padding:0.6rem;color:#888">No escalation history yet.</td></tr>';
  }
  for (const s of erows) {
    const tr = document.createElement('tr');
    tr.style.borderBottom = '1px solid #8882';
    tr.innerHTML =
      `<td style="padding:0.3rem 0">${s.source_name} → ${s.dest_name}</td>` +
      `<td>${s.car_name}</td>` +
      `<td>${s.samples}</td>` +
      `<td>${s.median_steps.toFixed(1)}</td>` +
      `<td>₹${s.p10_final.toLocaleString()}</td>` +
      `<td>₹${s.p50_final.toLocaleString()}</td>` +
      `<td>₹${s.p90_final.toLocaleString()}</td>` +
      `<td>${Math.round(s.take_rate*100)}%</td>`;
    etbody.appendChild(tr);
  }

  // Escalation form initial state.
  const ef = document.getElementById('esc-form');
  ef.annotate_escalation.checked = ec.annotate_escalation;
  ef.suppress_below_p50.checked   = ec.suppress_below_p50;
}

document.getElementById('esc-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const body = {
    annotate_escalation: ev.target.annotate_escalation.checked,
    suppress_below_p50:  ev.target.suppress_below_p50.checked,
  };
  const r = await fetch('/api/escalation-config', {
    method: 'POST', headers: {'content-type':'application/json'},
    body: JSON.stringify(body),
  });
  if (r.ok) alert('Saved.');
  else alert('Save failed: ' + (await r.text()));
});

document.getElementById('analytics-btn').addEventListener('click', () => {
  const a = document.getElementById('analytics');
  if (a.classList.contains('hidden')) {
    loadAnalytics().then(() => a.classList.remove('hidden'));
  } else {
    a.classList.add('hidden');
  }
});
document.getElementById('ana-refresh').addEventListener('click', loadAnalytics);

document.getElementById('ana-preview-btn').addEventListener('click', async () => {
  const days = parseInt(document.getElementById('ana-days').value || '7', 10);
  const r = await fetch('/api/weekly-report?days=' + days);
  const j = await r.json();
  const pre = document.getElementById('ana-preview');
  pre.style.display = 'block';
  pre.textContent = j.text || j.detail || 'no data';
});

document.getElementById('ana-send-btn').addEventListener('click', async () => {
  const days = parseInt(document.getElementById('ana-days').value || '7', 10);
  const r = await fetch('/api/weekly-report/send?days=' + days, {method:'POST'});
  const j = await r.json();
  alert(j.detail || (j.ok ? `Sent (${j.lines} lines).` : 'failed'));
});

async function loadFleet() {
  const [fr, sr, cr] = await Promise.all([
    fetch('/api/fleet'),
    fetch('/api/status'),
    fetch('/api/cars'),
  ]);
  const cars = (await fr.json()).cars || [];
  const status = await sr.json();
  const carTypes = (await cr.json()).cars || [];

  const tbody = document.querySelector('#fleet-table tbody');
  tbody.innerHTML = '';
  if (cars.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" style="padding:0.6rem;color:#888">No cars added yet.</td></tr>';
  }
  for (const c of cars) {
    const tr = document.createElement('tr');
    tr.style.borderBottom = '1px solid #8882';
    const coords = (c.location_lat != null && c.location_lng != null)
      ? `${c.location_lat.toFixed(4)}, ${c.location_lng.toFixed(4)}`
      : '<i style="color:#c47b00">not geocoded</i>';
    tr.innerHTML =
      `<td style="padding:0.4rem 0">${c.label}</td>` +
      `<td>${c.car_type_id || '<i>any</i>'}</td>` +
      `<td>${c.location_text || ''}</td>` +
      `<td>${coords}</td>` +
      `<td><button data-id="${c.id}" class="del">Delete</button></td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll('button.del').forEach(b => {
    b.addEventListener('click', async () => {
      if (!confirm('Delete this car?')) return;
      await fetch('/api/fleet/' + b.dataset.id, {method: 'DELETE'});
      loadFleet();
    });
  });

  // Populate the car-type dropdown.
  const sel = document.querySelector('#fleet-add-form select[name=car_type_id]');
  sel.innerHTML = '<option value="">Any (no filter)</option>';
  for (const ct of carTypes) {
    const opt = document.createElement('option');
    opt.value = ct.car_type_id;
    opt.textContent = `${ct.car_name} (${ct.car_type_id})`;
    sel.appendChild(opt);
  }

  // Toggle form initial values.
  const tf = document.getElementById('fleet-toggle-form');
  // We don't expose enable_deadhead in /api/status, fetch via /api/fleet-config.
  const fc = await (await fetch('/api/fleet-config')).json();
  tf.enable_deadhead.checked  = fc.enable_deadhead;
  tf.nominatim_user_agent.value = fc.nominatim_user_agent || '';
}

document.getElementById('fleet-btn').addEventListener('click', () => {
  const f = document.getElementById('fleet');
  if (f.classList.contains('hidden')) {
    loadFleet().then(() => f.classList.remove('hidden'));
  } else {
    f.classList.add('hidden');
  }
});

document.getElementById('fleet-sync-btn').addEventListener('click', async () => {
  const btn = document.getElementById('fleet-sync-btn');
  const out = document.getElementById('fleet-sync-result');
  btn.disabled = true;
  out.textContent = 'syncing…';
  try {
    const r = await fetch('/api/fleet/sync', {method: 'POST'});
    const j = await r.json();
    if (r.ok) {
      const c = j.cars || {};
      out.textContent =
        `cars: +${c.inserted||0} new · ${c.updated||0} refreshed · ` +
        `${c.skipped_inactive||0} inactive · drivers cached: ${j.drivers||0}`;
      loadFleet();
    } else {
      out.textContent = 'error: ' + (j.detail || 'failed');
    }
  } catch (e) {
    out.textContent = 'error: ' + e;
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('fleet-add-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const body = {
    label:         fd.get('label'),
    car_type_id:   fd.get('car_type_id') || null,
    location_text: fd.get('location_text'),
  };
  const msg = document.getElementById('fleet-msg');
  msg.style.display = 'block';
  msg.textContent = 'geocoding…';
  const r = await fetch('/api/fleet', {
    method: 'POST', headers: {'content-type':'application/json'},
    body: JSON.stringify(body),
  });
  const j = await r.json();
  msg.textContent = JSON.stringify(j, null, 2);
  if (r.ok) {
    ev.target.reset();
    loadFleet();
  }
});

document.getElementById('fleet-toggle-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const body = {
    enable_deadhead:      ev.target.enable_deadhead.checked,
    nominatim_user_agent: fd.get('nominatim_user_agent'),
  };
  const r = await fetch('/api/fleet-config', {
    method: 'POST', headers: {'content-type':'application/json'},
    body: JSON.stringify(body),
  });
  if (r.ok) alert('Saved.');
  else alert('Save failed: ' + (await r.text()));
});

document.getElementById('profit-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const fuel_overrides = {};
  const driver_overrides = {};
  // Per-car-type override field names are fuel_<id> / driver_<id> where
  // <id> is a numeric Savaari car_type_id. Match the digit suffix
  // explicitly so the global defaults (fuel_rate_default,
  // driver_pct_default) don't sneak into the override dicts.
  for (const [k, v] of fd.entries()) {
    if (v === '' || v == null) continue;
    let m = k.match(/^fuel_(\d+)$/);
    if (m) { fuel_overrides[m[1]] = parseFloat(v); continue; }
    m = k.match(/^driver_(\d+)$/);
    if (m) { driver_overrides[m[1]] = parseFloat(v); }
  }
  const body = {
    fuel_rate_default:        parseFloat(fd.get('fuel_rate_default')),
    driver_pct_default:       parseFloat(fd.get('driver_pct_default')),
    fuel_rate_per_car_type:   fuel_overrides,
    driver_pct_per_car_type:  driver_overrides,
  };
  const r = await fetch('/api/profit-config', {
    method: 'POST', headers: {'content-type':'application/json'},
    body: JSON.stringify(body),
  });
  if (r.ok) alert('Saved.');
  else alert('Save failed: ' + (await r.text()));
});

document.getElementById('pause-btn').addEventListener('click', async () => {
  await fetch('/api/pause', {method: 'POST'});
  refresh();
});
document.getElementById('quit-btn').addEventListener('click', async () => {
  if (!confirm('Quit Savaari Bot?')) return;
  await fetch('/api/quit', {method: 'POST'});
  document.getElementById('subtitle').textContent = 'shutting down…';
});
document.getElementById('settings-btn').addEventListener('click', () => {
  document.getElementById('wizard').classList.toggle('hidden');
});

refresh();
setInterval(refresh, 3000);
</script>
</body></html>
"""


def make_app(state: AppState) -> FastAPI:
    app = FastAPI(title="Savaari Bot", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return INDEX_HTML

    @app.get("/api/status")
    async def status() -> dict:
        # The DB connection lives on the Worker; we open a short-lived one
        # here for the per-request count query so route handlers stay
        # thread-safe with the poller.
        try:
            conn = db.open_db(state.cfg.db_path)
            counts = db.counts_today(conn)
            conn.close()
        except Exception:
            counts = {"alerts_today": 0, "confirms_today": 0}
        return state.snapshot(counts)

    @app.post("/api/config")
    async def save_config(req: Request):
        body = await req.json()
        cfg = state.cfg
        if "vendor_token" in body and body["vendor_token"]:
            cfg.vendor_token = body["vendor_token"].strip()
        if "telegram_bot_token" in body:
            cfg.telegram_bot_token = body["telegram_bot_token"].strip()
        if "telegram_chat_id" in body:
            cfg.telegram_chat_id = body["telegram_chat_id"].strip()
        if "poll_interval_s" in body and body["poll_interval_s"]:
            cfg.poll_interval_s = float(body["poll_interval_s"])
        if "fare_floor" in body and body["fare_floor"] != "":
            cfg.fare_floor = int(body["fare_floor"])
        if "fare_floor_basis" in body and body["fare_floor_basis"]:
            v = str(body["fare_floor_basis"]).lower()
            if v in ("net", "gross"):
                cfg.fare_floor_basis = v
        if "dry_run_accept" in body:
            cfg.dry_run_accept = bool(body["dry_run_accept"])
        config.save(cfg)
        state.mark_config_dirty()
        log.info("config saved via dashboard (dry_run=%s)", cfg.dry_run_accept)
        return JSONResponse({"ok": True})

    @app.get("/api/cars")
    async def cars():
        try:
            conn = db.open_db(state.cfg.db_path)
            items = db.list_car_types(conn)
            conn.close()
        except Exception:
            items = []
        return {"cars": items}

    @app.get("/api/profit-config")
    async def get_profit_config():
        cfg = state.cfg
        return {
            "fuel_rate_default": cfg.fuel_rate_default,
            "driver_pct_default": cfg.driver_pct_default,
            "fuel_rate_per_car_type": cfg.fuel_rate_per_car_type,
            "driver_pct_per_car_type": cfg.driver_pct_per_car_type,
        }

    @app.post("/api/profit-config")
    async def save_profit_config(req: Request):
        body = await req.json()
        cfg = state.cfg
        if "fuel_rate_default" in body:
            cfg.fuel_rate_default = float(body["fuel_rate_default"])
        if "driver_pct_default" in body:
            cfg.driver_pct_default = float(body["driver_pct_default"])
        if isinstance(body.get("fuel_rate_per_car_type"), dict):
            cfg.fuel_rate_per_car_type = {
                str(k): float(v) for k, v in body["fuel_rate_per_car_type"].items()
            }
        if isinstance(body.get("driver_pct_per_car_type"), dict):
            cfg.driver_pct_per_car_type = {
                str(k): float(v) for k, v in body["driver_pct_per_car_type"].items()
            }
        config.save(cfg)
        log.info("profit config saved via dashboard")
        return JSONResponse({"ok": True})

    @app.get("/api/gate-config")
    async def get_gate_config():
        cfg = state.cfg
        return {
            "vendor_user_id": cfg.vendor_user_id,
            "annotate_eligibility": cfg.annotate_eligibility,
            "require_eligible_car": cfg.require_eligible_car,
            "eligibility_cache_ttl_s": cfg.eligibility_cache_ttl_s,
        }

    @app.post("/api/gate-config")
    async def save_gate_config(req: Request):
        body = await req.json()
        cfg = state.cfg
        if "annotate_eligibility" in body:
            cfg.annotate_eligibility = bool(body["annotate_eligibility"])
        if "require_eligible_car" in body:
            cfg.require_eligible_car = bool(body["require_eligible_car"])
        if "eligibility_cache_ttl_s" in body:
            cfg.eligibility_cache_ttl_s = float(body["eligibility_cache_ttl_s"])
        config.save(cfg)
        state.mark_config_dirty()
        log.info(
            "gate config saved: annotate=%s require=%s ttl=%ss",
            cfg.annotate_eligibility,
            cfg.require_eligible_car,
            cfg.eligibility_cache_ttl_s,
        )
        return JSONResponse({"ok": True})

    @app.get("/api/fleet")
    async def fleet_list():
        try:
            conn = db.open_db(state.cfg.db_path)
            cars = [c.to_dict() for c in fleet.list_cars(conn)]
            conn.close()
        except Exception as e:
            return JSONResponse({"detail": f"failed: {e}"}, status_code=500)
        return {"cars": cars}

    @app.post("/api/fleet")
    async def fleet_add(req: Request):
        body = await req.json()
        label = (body.get("label") or "").strip()
        if not label:
            return JSONResponse({"detail": "label required"}, status_code=400)
        location_text = (body.get("location_text") or "").strip()
        car_type_id = body.get("car_type_id") or None
        worker = getattr(state, "worker", None)
        # Try to geocode immediately so the user gets a clear pass/fail
        # rather than silent "not geocoded" rows.
        lat, lng = None, None
        geocode_warn = ""
        if location_text and worker and worker._geocoder is not None:
            try:
                g = await worker._geocoder.geocode(location_text)
                if g is not None:
                    lat, lng = g.lat, g.lng
                else:
                    geocode_warn = (
                        "geocode returned no result — check the address, or "
                        "Nominatim may have rejected the User-Agent (avoid the "
                        "word 'test' and use a real contact email)"
                    )
            except Exception as e:
                log.exception("geocode failed during fleet_add")
                geocode_warn = f"geocode crashed: {type(e).__name__}: {e}"
        try:
            conn = db.open_db(state.cfg.db_path)
            new_id = fleet.upsert_car(
                conn,
                label=label,
                car_type_id=car_type_id,
                location_text=location_text,
                location_lat=lat,
                location_lng=lng,
            )
            conn.close()
        except Exception as e:
            return JSONResponse({"detail": f"insert failed: {e}"}, status_code=500)
        out: dict = {"ok": True, "id": new_id, "lat": lat, "lng": lng}
        if geocode_warn:
            out["warn"] = geocode_warn
        return out

    @app.post("/api/fleet/sync")
    async def fleet_sync():
        worker = getattr(state, "worker", None)
        if worker is None:
            return JSONResponse({"detail": "worker not ready"}, status_code=503)
        return await worker.sync_fleet_from_savaari()

    @app.delete("/api/fleet/{car_id}")
    async def fleet_delete(car_id: int):
        try:
            conn = db.open_db(state.cfg.db_path)
            ok = fleet.delete_car(conn, car_id)
            conn.close()
        except Exception as e:
            return JSONResponse({"detail": f"delete failed: {e}"}, status_code=500)
        if not ok:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return {"ok": True}

    @app.get("/api/fleet-config")
    async def get_fleet_config():
        cfg = state.cfg
        return {
            "enable_deadhead": cfg.enable_deadhead,
            "nominatim_user_agent": cfg.nominatim_user_agent,
            "nominatim_base": cfg.nominatim_base,
            "osrm_base": cfg.osrm_base,
        }

    @app.post("/api/fleet-config")
    async def save_fleet_config(req: Request):
        body = await req.json()
        cfg = state.cfg
        if "enable_deadhead" in body:
            cfg.enable_deadhead = bool(body["enable_deadhead"])
        if "nominatim_user_agent" in body and body["nominatim_user_agent"]:
            cfg.nominatim_user_agent = str(body["nominatim_user_agent"])[:200]
        if "nominatim_base" in body and body["nominatim_base"]:
            cfg.nominatim_base = str(body["nominatim_base"]).rstrip("/")
        if "osrm_base" in body and body["osrm_base"]:
            cfg.osrm_base = str(body["osrm_base"]).rstrip("/")
        config.save(cfg)
        state.mark_config_dirty()
        log.info("fleet config saved (deadhead=%s)", cfg.enable_deadhead)
        return JSONResponse({"ok": True})

    @app.get("/api/analytics")
    async def get_analytics(days: int = 14):
        try:
            conn = db.open_db(state.cfg.db_path)
            from . import analytics as ana_mod
            from .weekly_report import _car_types_lookup, _headline_counts
            rows = ana_mod.query_route_stats(conn, days=days, min_samples=1)
            cities = db.cities_lookup(conn)
            car_types = _car_types_lookup(conn)
            headline = _headline_counts(conn, days)
            conn.close()
        except Exception as e:
            return JSONResponse({"detail": f"failed: {e}"}, status_code=500)
        out_rows = [
            {
                "source_city": s.source_city,
                "dest_city": s.dest_city,
                "car_type_id": s.car_type_id,
                "source_name": cities.get(s.source_city, s.source_city),
                "dest_name": cities.get(s.dest_city, s.dest_city),
                "car_name": car_types.get(s.car_type_id, s.car_type_id or "any"),
                "samples": s.samples,
                "avg_responders": s.avg_responders,
                "max_responders": s.max_responders,
                "take_rate": s.take_rate,
                "avg_first_fare": s.avg_first_fare,
                "avg_max_fare": s.avg_max_fare,
                "avg_escalation": s.avg_escalation,
            }
            for s in rows
        ]
        return {"days": days, "headline": headline, "routes": out_rows}

    @app.get("/api/escalation")
    async def get_escalation(days: int = 14):
        try:
            conn = db.open_db(state.cfg.db_path)
            from . import escalation as esc_mod
            from .weekly_report import _car_types_lookup
            rows = esc_mod.query_escalation_stats(conn, days=days, min_samples=1)
            cities = db.cities_lookup(conn)
            car_types = _car_types_lookup(conn)
            conn.close()
        except Exception as e:
            return JSONResponse({"detail": f"failed: {e}"}, status_code=500)
        out_rows = [
            {
                "source_city": s.source_city,
                "dest_city": s.dest_city,
                "car_type_id": s.car_type_id,
                "source_name": cities.get(s.source_city, s.source_city),
                "dest_name": cities.get(s.dest_city, s.dest_city),
                "car_name": car_types.get(s.car_type_id, s.car_type_id or "any"),
                "samples": s.samples,
                "median_steps": s.median_steps,
                "p10_final": s.p10_final,
                "p50_final": s.p50_final,
                "p90_final": s.p90_final,
                "take_rate": s.take_rate,
            }
            for s in rows
        ]
        return {"days": days, "routes": out_rows}

    @app.get("/api/escalation-config")
    async def get_escalation_config():
        cfg = state.cfg
        return {
            "annotate_escalation": cfg.annotate_escalation,
            "suppress_below_p50": cfg.suppress_below_p50,
        }

    @app.post("/api/escalation-config")
    async def save_escalation_config(req: Request):
        body = await req.json()
        cfg = state.cfg
        if "annotate_escalation" in body:
            cfg.annotate_escalation = bool(body["annotate_escalation"])
        if "suppress_below_p50" in body:
            cfg.suppress_below_p50 = bool(body["suppress_below_p50"])
        config.save(cfg)
        log.info(
            "escalation config saved: annotate=%s suppress=%s",
            cfg.annotate_escalation, cfg.suppress_below_p50,
        )
        return JSONResponse({"ok": True})

    @app.get("/api/weekly-report")
    async def get_weekly_report(days: int = 7):
        worker = getattr(state, "worker", None)
        if worker is None:
            return JSONResponse({"detail": "worker not ready"}, status_code=503)
        try:
            rep = worker.build_weekly_report(days=days)
        except Exception as e:
            return JSONResponse({"detail": f"failed: {e}"}, status_code=500)
        return {"days": days, "text": rep.to_text(), "html": rep.to_html()}

    @app.post("/api/weekly-report/send")
    async def send_weekly_report(days: int = 7):
        worker = getattr(state, "worker", None)
        if worker is None:
            return JSONResponse({"detail": "worker not ready"}, status_code=503)
        return await worker.send_weekly_report_now(days=days)

    @app.post("/api/test-availability")
    async def test_availability():
        worker = getattr(state, "worker", None)
        if worker is None:
            return JSONResponse({"detail": "worker not ready"}, status_code=503)
        return await worker.test_availability()

    @app.post("/api/test-alert")
    async def test_alert():
        worker = getattr(state, "worker", None)
        if worker is None:
            return JSONResponse({"detail": "worker not ready"}, status_code=503)
        try:
            result = await worker.send_test_alert()
        except Exception as e:
            return JSONResponse({"detail": f"failed: {e}"}, status_code=500)
        return {"result": result}

    @app.post("/api/pause")
    async def pause():
        state.paused = not state.paused
        log.info("paused=%s", state.paused)
        return {"paused": state.paused}

    @app.post("/api/quit")
    async def quit_():
        log.info("shutdown requested via dashboard")
        state.request_shutdown()
        return {"ok": True}

    return app
