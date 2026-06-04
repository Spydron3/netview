'use strict';

let allDevices = [];
let pollTimer = null;
let currentTab = 'devices';

// ── bootstrap ─────────────────────────────────────────────────────────────────

(async function init() {
  await Promise.all([loadStats(), loadDevices(), loadHistory()]);
  startAutoRefresh();
})();

// ── tab switching ─────────────────────────────────────────────────────────────

function switchTab(name) {
  currentTab = name;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(tc => tc.classList.toggle('hidden', tc.id !== `tab-${name}`));
  if (name === 'topology') loadTopologyTab();
}

// ── settings ──────────────────────────────────────────────────────────────────

async function openSettings() {
  try {
    const s = await apiFetch('/api/settings');
    el('s-interval').value    = Math.round(parseInt(s.scan_interval || '300') / 60);
    el('s-network').value     = s.network_range || '';
    el('s-port-scan').checked = (s.port_scan_enabled || 'true') === 'true';
    el('settings-msg').textContent = '';
  } catch (e) {
    console.error('loadSettings:', e);
  }
  el('settings-modal').classList.remove('hidden');
}

function closeSettings() {
  el('settings-modal').classList.add('hidden');
}

async function saveSettings() {
  const minutes = parseInt(el('s-interval').value);
  if (!minutes || minutes < 1) {
    showSettingsMsg('Interval must be at least 1 minute.', true);
    return;
  }
  try {
    await apiFetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scan_interval:      minutes * 60,
        port_scan_enabled:  el('s-port-scan').checked,
        network_range:      el('s-network').value.trim(),
      }),
    });
    showSettingsMsg('Saved.');
    setTimeout(closeSettings, 800);
  } catch (e) {
    showSettingsMsg('Failed to save.', true);
  }
}

function showSettingsMsg(text, error = false) {
  const msg = el('settings-msg');
  msg.textContent = text;
  msg.style.color = error ? 'var(--red)' : 'var(--green)';
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeSettings(); closeTopoLog(); }
});

// ── data loading ──────────────────────────────────────────────────────────────

async function loadStats() {
  try {
    const s = await apiFetch('/api/stats');
    el('stat-total').textContent   = s.total_devices;
    el('stat-online').textContent  = s.online_devices;
    el('stat-offline').textContent = s.offline_devices;
    el('network-badge').textContent = s.network_range || 'detecting…';

    if (s.last_scan?.finished_at) {
      el('stat-last-scan').textContent = timeAgo(new Date(s.last_scan.finished_at + 'Z'));
    }

    setScanRunning(s.scan_running);
  } catch (e) {
    console.error('loadStats:', e);
  }
}

async function loadDevices() {
  try {
    allDevices = await apiFetch('/api/devices');
    applyFilter();
  } catch (e) {
    console.error('loadDevices:', e);
  }
}

async function loadHistory() {
  try {
    const rows = await apiFetch('/api/scan/history');
    const tbody = el('history-body');
    if (!rows.length) { tbody.innerHTML = '<tr><td colspan="6" style="color:var(--muted)">No scans yet</td></tr>'; return; }
    tbody.innerHTML = rows.map(r => `
      <tr>
        <td>${r.started_at ? fmt(new Date(r.started_at + 'Z')) : '—'}</td>
        <td><code>${r.network_range || '—'}</code></td>
        <td>${r.devices_found}</td>
        <td>${r.devices_online}</td>
        <td>${r.duration_seconds != null ? r.duration_seconds.toFixed(1) + 's' : '—'}</td>
        <td><span class="status-pill ${r.status}">${r.status}</span></td>
      </tr>`).join('');
  } catch (e) {
    console.error('loadHistory:', e);
  }
}

// ── filtering & rendering ─────────────────────────────────────────────────────

function applyFilter() {
  const q    = el('search').value.toLowerCase();
  const showOffline = el('show-offline').checked;

  const filtered = allDevices.filter(d => {
    if (!showOffline && !d.is_online) return false;
    if (!q) return true;
    return (
      (d.ip_address  || '').includes(q) ||
      (d.name        || '').toLowerCase().includes(q) ||
      (d.hostname    || '').toLowerCase().includes(q) ||
      (d.mac_address || '').toLowerCase().includes(q) ||
      (d.vendor      || '').toLowerCase().includes(q)
    );
  });

  renderDevices(filtered);
}

function renderDevices(devices) {
  const grid = el('device-grid');
  if (!devices.length) {
    grid.innerHTML = '<p class="empty">No devices match</p>';
    return;
  }

  grid.innerHTML = devices.map(d => {
    const statusClass = d.is_online ? 'online' : 'offline';
    const ports = (d.open_ports || []).slice(0, 6);
    const extra = (d.open_ports || []).length - ports.length;

    return `<div class="device-card ${d.is_online ? '' : 'offline'}">
      <div class="device-top">
        <div class="status-dot ${statusClass}"></div>
        <span class="device-ip">${esc(d.ip_address)}</span>
      </div>
      <div class="device-name-row" onclick="startEditName(${d.id}, this)" title="Click to set a name">
        ${d.name
          ? `<span class="device-name">${esc(d.name)}</span>`
          : `<span class="device-name-placeholder">+ Add name</span>`}
      </div>
      ${d.hostname ? `<div class="device-hostname">${esc(d.hostname)}</div>` : ''}
      ${d.vendor   ? `<div class="device-vendor">${esc(d.vendor)}</div>` : ''}
      ${d.mac_address ? `<div class="device-mac">${esc(d.mac_address)}</div>` : ''}
      ${ports.length ? `
        <div class="ports">
          ${ports.map(p => `<span class="port-chip" title="${esc(p.service || p.protocol)}">${p.port}</span>`).join('')}
          ${extra > 0 ? `<span class="port-more">+${extra} more</span>` : ''}
        </div>` : ''}
      <div class="device-footer">
        ${d.is_online
          ? `Online · seen ${timeAgo(new Date(d.last_seen + 'Z'))}`
          : `Offline · last seen ${timeAgo(new Date(d.last_seen + 'Z'))}`}
      </div>
    </div>`;
  }).join('');
}

// ── name editing ─────────────────────────────────────────────────────────────

function startEditName(deviceId, row) {
  if (row.querySelector('input')) return; // already editing
  const dev = allDevices.find(d => d.id === deviceId);
  const current = dev?.name || '';

  const input = document.createElement('input');
  input.className = 'name-input';
  input.value = current;
  input.placeholder = 'Device name…';
  row.innerHTML = '';
  row.appendChild(input);
  input.focus();
  input.select();

  let saved = false;

  async function save() {
    if (saved) return;
    saved = true;
    const name = input.value.trim() || null;
    try {
      await apiFetch(`/api/devices/${deviceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (dev) dev.name = name;
    } catch (e) {
      console.error('Failed to save name:', e);
    }
    applyFilter();
  }

  input.addEventListener('blur', save);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { input.blur(); }
    if (e.key === 'Escape') { saved = true; applyFilter(); }
  });
}

// ── scan trigger ──────────────────────────────────────────────────────────────

async function triggerScan() {
  el('scan-btn').disabled = true;
  try {
    await apiFetch('/api/scan', { method: 'POST' });
    setScanRunning(true);
    pollScan();
  } catch (e) {
    console.error('triggerScan:', e);
    el('scan-btn').disabled = false;
  }
}

function pollScan() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      const status = await apiFetch('/api/scan/status');
      await loadStats();
      await loadDevices();
      if (status.running) {
        pollScan();
      } else {
        await loadHistory();
        setScanRunning(false);
      }
    } catch (e) {
      console.error('pollScan:', e);
      setScanRunning(false);
    }
  }, 3000);
}

function setScanRunning(running) {
  const btn    = el('scan-btn');
  const banner = el('scan-banner');
  btn.disabled      = running;
  btn.textContent   = running ? 'Scanning…' : 'Scan Now';
  banner.classList.toggle('hidden', !running);
}

// ── auto-refresh ──────────────────────────────────────────────────────────────

function startAutoRefresh() {
  setInterval(async () => {
    await Promise.all([loadStats(), loadDevices()]);
  }, 30_000);
}

// ── helpers ───────────────────────────────────────────────────────────────────

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function el(id) { return document.getElementById(id); }

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function timeAgo(date) {
  const s = Math.floor((Date.now() - date) / 1000);
  if (s < 5)    return 'just now';
  if (s < 60)   return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function fmt(date) {
  return date.toLocaleString(undefined, {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

// ── topology tab ──────────────────────────────────────────────────────────────

let topoSimulation = null;
let topoPollTimer  = null;

async function loadTopologyTab() {
  await Promise.all([loadSwitches(), loadTopology()]);
}

// ── switch management ─────────────────────────────────────────────────────────

async function loadSwitches() {
  try {
    const switches = await apiFetch('/api/switches');
    renderSwitches(switches);
  } catch (e) { console.error('loadSwitches:', e); }
}

function renderSwitches(switches) {
  const list = el('switches-list');
  if (!switches.length) {
    list.innerHTML = '<p class="empty-sw">No switches added yet.</p>';
    return;
  }
  list.innerHTML = switches.map(sw => `
    <div class="switch-row">
      <div class="switch-info">
        <span class="switch-status-dot ${sw.status}"></span>
        <div>
          <div class="switch-name">${esc(sw.name || sw.ip_address)}</div>
          <div class="switch-meta">${sw.name ? esc(sw.ip_address) + ' · ' : ''}community: ${esc(sw.community)}
            ${sw.last_polled ? ' · polled ' + timeAgo(new Date(sw.last_polled + 'Z')) : ''}
          </div>
        </div>
      </div>
      <button class="btn-delete" onclick="deleteSwitch(${sw.id})" title="Remove switch">✕</button>
    </div>`).join('');
}

function showAddSwitchForm() {
  el('add-switch-form').classList.remove('hidden');
  el('sw-ip').focus();
}
function hideAddSwitchForm() {
  el('add-switch-form').classList.add('hidden');
  el('sw-ip').value = '';
  el('sw-name').value = '';
  el('sw-community').value = '';
  el('sw-form-error').textContent = '';
}

async function addSwitch() {
  const ip = el('sw-ip').value.trim();
  if (!ip) { el('sw-form-error').textContent = 'IP address is required.'; return; }
  try {
    await apiFetch('/api/switches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ip_address: ip,
        name: el('sw-name').value.trim() || null,
        community: el('sw-community').value.trim() || 'public',
      }),
    });
    hideAddSwitchForm();
    await loadSwitches();
  } catch (e) {
    el('sw-form-error').textContent = e.message || 'Failed to add switch.';
  }
}

async function deleteSwitch(id) {
  if (!confirm('Remove this switch and its topology data?')) return;
  try {
    await apiFetch(`/api/switches/${id}`, { method: 'DELETE' });
    await loadSwitches();
    await loadTopology();
  } catch (e) { console.error('deleteSwitch:', e); }
}

// ── topology scan trigger ─────────────────────────────────────────────────────

async function triggerTopoScan() {
  el('topo-scan-btn').disabled = true;
  try {
    await apiFetch('/api/topology/scan', { method: 'POST' });
    setTopoScanning(true);
    openTopoLog();
    pollTopoScan();
  } catch (e) {
    el('topo-scan-btn').disabled = false;
  }
}

function pollTopoScan() {
  clearTimeout(topoPollTimer);
  topoPollTimer = setTimeout(async () => {
    const status = await apiFetch('/api/topology/status');
    if (status.running) {
      pollTopoScan();
    } else {
      setTopoScanning(false);
      await Promise.all([loadSwitches(), loadTopology()]);
    }
  }, 3000);
}

function setTopoScanning(running) {
  const btn = el('topo-scan-btn');
  btn.disabled = running;
  btn.textContent = running ? 'Scanning…' : 'Scan Topology';
  el('topo-banner').classList.toggle('hidden', !running);
}

// ── topology log modal ────────────────────────────────────────────────────────

let _logPollTimer = null;

function openTopoLog() {
  el('topo-log-modal').classList.remove('hidden');
  refreshTopoLog();
}

function closeTopoLog() {
  el('topo-log-modal').classList.add('hidden');
  clearTimeout(_logPollTimer);
  _logPollTimer = null;
}

async function refreshTopoLog() {
  clearTimeout(_logPollTimer);
  try {
    const data = await apiFetch('/api/topology/log');
    const pre  = el('topo-log-pre');
    const badge = el('topo-log-status');
    const atBottom = pre.scrollHeight - pre.scrollTop <= pre.clientHeight + 40;

    pre.textContent = data.lines.length
      ? data.lines.join('\n')
      : '— no log yet —';

    badge.textContent = data.running ? 'running' : 'done';
    badge.className   = 'badge ' + (data.running ? 'badge-running' : 'badge-done');

    if (atBottom) pre.scrollTop = pre.scrollHeight;

    if (data.running) {
      _logPollTimer = setTimeout(refreshTopoLog, 1000);
    }
  } catch (e) {
    _logPollTimer = setTimeout(refreshTopoLog, 2000);
  }
}

// ── topology graph ────────────────────────────────────────────────────────────

async function loadTopology() {
  try {
    const data = await apiFetch('/api/topology');
    renderTopology(data);
  } catch (e) { console.error('loadTopology:', e); }
}

function renderTopology(data) {
  const wrap = el('topo-graph-wrap');
  const svgEl = el('topo-svg');
  const empty = el('topo-empty');

  if (!data.nodes.length) {
    empty.classList.remove('hidden');
    svgEl.style.display = 'none';
    return;
  }
  empty.classList.add('hidden');
  svgEl.style.display = '';

  const W = wrap.clientWidth  || 900;
  const H = wrap.clientHeight || 600;

  const svg = d3.select(svgEl).attr('width', W).attr('height', H);
  svg.selectAll('*').remove();

  // zoom
  const g = svg.append('g');
  svg.call(
    d3.zoom().scaleExtent([0.15, 4])
      .on('zoom', ev => g.attr('transform', ev.transform))
  );

  // deep-copy nodes/edges so D3 can mutate x/y
  const nodes = data.nodes.map(n => ({ ...n }));
  const edges = data.edges.map(e => ({ ...e }));

  // ── simulation ──
  if (topoSimulation) topoSimulation.stop();
  topoSimulation = d3.forceSimulation(nodes)
    .force('link',      d3.forceLink(edges).id(d => d.id).distance(d => d.type === 'lldp' ? 180 : 110))
    .force('charge',    d3.forceManyBody().strength(d => d.type === 'switch' ? -600 : -250))
    .force('center',    d3.forceCenter(W / 2, H / 2))
    .force('collision', d3.forceCollide(42));

  // ── edges ──
  const edgeG = g.append('g').attr('class', 'topo-edges');
  const edge = edgeG.selectAll('line').data(edges).join('line')
    .attr('class', d => `topo-edge topo-edge-${d.type}`)
    .on('mouseenter', (ev, d) => showEdgeTip(ev, d))
    .on('mouseleave', hideEdgeTip);

  // ── nodes ──
  const nodeG = g.append('g').attr('class', 'topo-nodes');
  const node = nodeG.selectAll('g').data(nodes).join('g')
    .attr('class', d => `topo-node topo-node-${d.type}`)
    .call(d3.drag()
      .on('start', (ev, d) => { if (!ev.active) topoSimulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
      .on('end',   (ev, d) => { if (!ev.active) topoSimulation.alphaTarget(0); d.fx = null; d.fy = null; })
    )
    .on('click', (ev, d) => { ev.stopPropagation(); showNodeDetail(d); });

  // shapes
  node.filter(d => d.type === 'switch' || d.type === 'external_switch')
    .append('rect').attr('width', 52).attr('height', 34).attr('x', -26).attr('y', -17).attr('rx', 5);

  node.filter(d => d.type !== 'switch' && d.type !== 'external_switch')
    .append('circle').attr('r', 20);

  // labels
  node.append('text').attr('dy', d =>
    (d.type === 'switch' || d.type === 'external_switch') ? 28 : 33
  ).text(d => _truncate(d.label, 16));

  // tick
  topoSimulation.on('tick', () => {
    edge.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    node.attr('transform', d => `translate(${d.x},${d.y})`);
  });

  // click on background closes detail
  svg.on('click', closeNodeDetail);
}

// ── node detail panel ─────────────────────────────────────────────────────────

function showNodeDetail(d) {
  const panel = el('node-detail');
  const body  = el('node-detail-body');

  const rows = [];
  if (d.ip)       rows.push(['IP',       d.ip]);
  if (d.mac)      rows.push(['MAC',      d.mac]);
  if (d.hostname) rows.push(['Hostname', d.hostname]);
  if (d.vendor)   rows.push(['Vendor',   d.vendor]);
  if (d.name && d.name !== d.label) rows.push(['Name', d.name]);
  if (d.status)   rows.push(['SNMP status', d.status]);
  if (d.last_polled) rows.push(['Last polled', timeAgo(new Date(d.last_polled + 'Z'))]);
  if (d.sysname)  rows.push(['Sysname',  d.sysname]);

  const online = d.is_online === true ? '<span style="color:var(--green)">Online</span>'
               : d.is_online === false ? '<span style="color:var(--muted)">Offline</span>'
               : '';

  body.innerHTML = `
    <div class="nd-type">${d.type.replace('_', ' ')}</div>
    <div class="nd-label">${esc(d.label)}</div>
    ${online ? `<div class="nd-online">${online}</div>` : ''}
    <dl class="nd-props">
      ${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(String(v))}</dd>`).join('')}
    </dl>`;

  panel.classList.remove('hidden');
}

function closeNodeDetail() {
  el('node-detail').classList.add('hidden');
}

// ── edge tooltip ──────────────────────────────────────────────────────────────

let _edgeTip = null;

function showEdgeTip(ev, d) {
  if (!d.port) return;
  if (!_edgeTip) {
    _edgeTip = document.createElement('div');
    _edgeTip.className = 'edge-tooltip';
    document.body.appendChild(_edgeTip);
  }
  _edgeTip.textContent = d.port;
  _edgeTip.style.left = (ev.pageX + 12) + 'px';
  _edgeTip.style.top  = (ev.pageY - 8)  + 'px';
  _edgeTip.style.display = 'block';
}

function hideEdgeTip() {
  if (_edgeTip) _edgeTip.style.display = 'none';
}

function _truncate(s, n) {
  return s && s.length > n ? s.slice(0, n) + '…' : s;
}
