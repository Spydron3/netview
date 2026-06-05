'use strict';

let allDevices = [];
let allPorts   = [];   // flat list from GET /api/ports — used for device dropdowns
let pollTimer  = null;
let currentTab = 'devices';

// ── bootstrap ─────────────────────────────────────────────────────────────────

(async function init() {
  await Promise.all([loadStats(), loadDevices(), loadHistory(), loadAllPorts()]);
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
  if (e.key === 'Escape') { closeSettings(); closePortsModal(); closeDevicePortsModal(); }
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

async function loadAllPorts() {
  try {
    allPorts = await apiFetch('/api/ports');
  } catch (e) {
    console.error('loadAllPorts:', e);
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
      <button class="btn btn-sm btn-ghost device-ports-btn" onclick="openDevicePortsModal(${d.id})">
        ${(d.switch_ports || []).length > 0
          ? `${d.switch_ports.length} port${d.switch_ports.length !== 1 ? 's' : ''}`
          : 'Assign port'}
      </button>
      <div class="device-footer">
        ${d.is_online
          ? `Online · seen ${timeAgo(new Date(d.last_seen + 'Z'))}`
          : `Offline · last seen ${timeAgo(new Date(d.last_seen + 'Z'))}`}
      </div>
    </div>`;
  }).join('');
}

// ── device ports modal ───────────────────────────────────────────────────────

let _devicePortsModalId = null;

async function openDevicePortsModal(deviceId) {
  _devicePortsModalId = deviceId;
  el('dp-error').textContent = '';

  const dev = allDevices.find(d => d.id === deviceId);
  el('device-ports-modal-title').textContent = dev ? (dev.name || dev.ip_address) : 'Device';
  el('device-ports-modal-sub').textContent   = dev?.name ? dev.ip_address : '';

  // populate switch dropdown
  const switches = await apiFetch('/api/switches');
  el('dp-switch').innerHTML = '<option value="">— select switch —</option>' +
    switches.map(s => `<option value="${s.id}">${esc(s.name || s.ip_address)}</option>`).join('');
  el('dp-port').innerHTML = '<option value="">— select port —</option>';

  el('device-ports-modal').classList.remove('hidden');
  await refreshDevicePortsModal();
}

function closeDevicePortsModal() {
  el('device-ports-modal').classList.add('hidden');
  _devicePortsModalId = null;
}

async function refreshDevicePortsModal() {
  if (!_devicePortsModalId) return;
  const dev = await apiFetch(`/api/devices/${_devicePortsModalId}`);

  // keep allDevices in sync
  const idx = allDevices.findIndex(d => d.id === _devicePortsModalId);
  if (idx >= 0) allDevices[idx] = dev;

  const ports = dev.switch_ports || [];
  const tbody = el('device-ports-tbody');
  const empty = el('device-ports-empty');

  if (!ports.length) {
    tbody.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    tbody.innerHTML = ports.map(p => `
      <tr>
        <td>${esc(p.switch_name)}</td>
        <td>${esc(p.label || ('Port ' + p.port_number))}</td>
        <td><span class="port-type-badge">${esc(p.port_type)}</span></td>
        <td>${esc(p.speed)}</td>
        <td><button class="btn-delete" onclick="disconnectDevicePort(${p.id})" title="Remove">✕</button></td>
      </tr>`).join('');
  }
}

async function loadDevicePortOptions() {
  const switchId = el('dp-switch').value;
  const portSel  = el('dp-port');
  if (!switchId) { portSel.innerHTML = '<option value="">— select port —</option>'; return; }

  const ports = await apiFetch(`/api/switches/${switchId}/ports`);
  const dev = allDevices.find(d => d.id === _devicePortsModalId);
  const assignedIds = new Set((dev?.switch_ports || []).map(p => p.id));

  portSel.innerHTML = '<option value="">— select port —</option>' +
    ports.map(p => {
      if (assignedIds.has(p.id)) return '';
      const label = (p.label || ('Port ' + p.port_number)) + ` (${p.port_type}·${p.speed})`;
      const taken = p.device_id ? ' — in use' : '';
      return `<option value="${p.id}">${esc(label)}${taken}</option>`;
    }).join('');
}

async function connectDevicePort() {
  const portId = el('dp-port').value;
  el('dp-error').textContent = '';
  if (!portId) { el('dp-error').textContent = 'Select a port.'; return; }
  try {
    await apiFetch(`/api/devices/${_devicePortsModalId}/port`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ switch_port_id: parseInt(portId) }),
    });
    el('dp-switch').value = '';
    el('dp-port').innerHTML = '<option value="">— select port —</option>';
    await Promise.all([loadAllPorts(), refreshDevicePortsModal()]);
    applyFilter();
    if (currentTab === 'topology') loadTopology();
  } catch (e) {
    el('dp-error').textContent = e.message || 'Failed to connect.';
  }
}

async function disconnectDevicePort(portId) {
  try {
    await apiFetch(`/api/devices/${_devicePortsModalId}/ports/${portId}`, { method: 'DELETE' });
    await Promise.all([loadAllPorts(), refreshDevicePortsModal()]);
    applyFilter();
    if (currentTab === 'topology') loadTopology();
  } catch (e) { console.error('disconnectDevicePort:', e); }
}

// ── name editing ─────────────────────────────────────────────────────────────

function startEditName(deviceId, row) {
  if (row.querySelector('input')) return;
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
      const updated = await apiFetch(`/api/devices/${deviceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const idx = allDevices.findIndex(d => d.id === deviceId);
      if (idx >= 0) allDevices[idx] = updated;
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
    await Promise.all([loadStats(), loadDevices(), loadAllPorts()]);
  }, 30_000);
}

// ── topology tab ──────────────────────────────────────────────────────────────

let topoSimulation = null;

async function loadTopologyTab() {
  await Promise.all([loadSwitches(), loadSwitchLinks(), loadTopology()]);
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
  list.innerHTML = switches.map(sw => {
    const label = sw.name || sw.ip_address || sw.mac_address;
    const sub   = [sw.name ? (sw.ip_address || sw.mac_address) : null,
                   `${sw.port_count} port${sw.port_count !== 1 ? 's' : ''}`]
                  .filter(Boolean).join(' · ');
    return `
    <div class="switch-row">
      <div class="switch-info" onclick="openPortsModal(${sw.id})" style="cursor:pointer;flex:1">
        <div>
          <div class="switch-name">${esc(label)}</div>
          <div class="switch-meta">${esc(sub)}</div>
        </div>
      </div>
      <button class="btn btn-sm btn-ghost" onclick="openPortsModal(${sw.id})" title="Manage ports">Ports</button>
      <button class="btn-delete" onclick="deleteSwitch(${sw.id})" title="Remove switch">✕</button>
    </div>`;
  }).join('');
}

function showAddSwitchForm() {
  el('add-switch-form').classList.remove('hidden');
  el('sw-ip').focus();
}
function hideAddSwitchForm() {
  el('add-switch-form').classList.add('hidden');
  el('sw-ip').value = '';
  el('sw-mac').value = '';
  el('sw-name').value = '';
  el('sw-form-error').textContent = '';
}

async function addSwitch() {
  const ip  = el('sw-ip').value.trim()  || null;
  const mac = el('sw-mac').value.trim() || null;
  if (!ip && !mac) { el('sw-form-error').textContent = 'IP address or MAC address is required.'; return; }
  try {
    await apiFetch('/api/switches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip_address: ip, mac_address: mac, name: el('sw-name').value.trim() || null }),
    });
    hideAddSwitchForm();
    await loadSwitches();
  } catch (e) {
    el('sw-form-error').textContent = e.message || 'Failed to add switch.';
  }
}

async function deleteSwitch(id) {
  if (!confirm('Remove this switch and all its ports?')) return;
  try {
    await apiFetch(`/api/switches/${id}`, { method: 'DELETE' });
    await Promise.all([loadSwitches(), loadSwitchLinks(), loadAllPorts(), loadTopology()]);
    applyFilter();
  } catch (e) { console.error('deleteSwitch:', e); }
}

// ── switch links ──────────────────────────────────────────────────────────────

async function loadSwitchLinks() {
  try {
    const links = await apiFetch('/api/switch-links');
    renderSwitchLinks(links);
  } catch (e) { console.error('loadSwitchLinks:', e); }
}

function renderSwitchLinks(links) {
  const list = el('links-list');
  if (!links.length) {
    list.innerHTML = '<p class="empty-sw">No switch links defined.</p>';
    return;
  }
  list.innerHTML = links.map(lnk => {
    const a = lnk.port_a;
    const b = lnk.port_b;
    const labelA = `${esc(a.switch_name)}: ${esc(a.label || ('Port ' + a.port_number))} (${esc(a.port_type)}·${esc(a.speed)})`;
    const labelB = `${esc(b.switch_name)}: ${esc(b.label || ('Port ' + b.port_number))} (${esc(b.port_type)}·${esc(b.speed)})`;
    return `<div class="switch-row">
      <div class="link-info">
        <span class="link-side">${labelA}</span>
        <span class="link-arrow">↔</span>
        <span class="link-side">${labelB}</span>
      </div>
      <button class="btn-delete" onclick="deleteSwitchLink(${lnk.id})" title="Remove link">✕</button>
    </div>`;
  }).join('');
}

async function showAddLinkForm() {
  el('link-form-error').textContent = '';
  // populate switch dropdowns
  const switches = await apiFetch('/api/switches');
  const opts = '<option value="">— select switch —</option>' +
    switches.map(s => `<option value="${s.id}">${esc(s.name || s.ip_address)}</option>`).join('');
  el('link-sw-a').innerHTML = opts;
  el('link-sw-b').innerHTML = opts;
  el('link-port-a').innerHTML = '<option value="">— select port —</option>';
  el('link-port-b').innerHTML = '<option value="">— select port —</option>';
  el('add-link-form').classList.remove('hidden');
}

function hideAddLinkForm() {
  el('add-link-form').classList.add('hidden');
}

async function loadLinkPorts(side) {
  const swSel   = el(`link-sw-${side}`);
  const portSel = el(`link-port-${side}`);
  const switchId = swSel.value;
  if (!switchId) {
    portSel.innerHTML = '<option value="">— select port —</option>';
    return;
  }
  const ports = await apiFetch(`/api/switches/${switchId}/ports`);
  portSel.innerHTML = '<option value="">— select port —</option>' +
    ports.map(p => {
      const lbl = p.label || ('Port ' + p.port_number);
      return `<option value="${p.id}">${esc(lbl)} (${p.port_type} · ${p.speed})</option>`;
    }).join('');
}

async function addSwitchLink() {
  const portAId = el('link-port-a').value;
  const portBId = el('link-port-b').value;
  el('link-form-error').textContent = '';
  if (!portAId || !portBId) {
    el('link-form-error').textContent = 'Select a port on both sides.';
    return;
  }
  if (portAId === portBId) {
    el('link-form-error').textContent = 'Cannot link a port to itself.';
    return;
  }
  try {
    await apiFetch('/api/switch-links', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ port_a_id: parseInt(portAId), port_b_id: parseInt(portBId) }),
    });
    hideAddLinkForm();
    await Promise.all([loadSwitchLinks(), loadTopology()]);
  } catch (e) {
    el('link-form-error').textContent = e.message || 'Failed to save link.';
  }
}

async function deleteSwitchLink(id) {
  try {
    await apiFetch(`/api/switch-links/${id}`, { method: 'DELETE' });
    await Promise.all([loadSwitchLinks(), loadTopology()]);
  } catch (e) { console.error('deleteSwitchLink:', e); }
}

// ── ports modal ───────────────────────────────────────────────────────────────

let _portsModalSwitchId = null;
let _portsModalDevices  = [];

async function openPortsModal(switchId) {
  _portsModalSwitchId = switchId;
  el('ports-modal').classList.remove('hidden');
  el('ap-error').textContent = '';

  // load switch info for header
  const switches = await apiFetch('/api/switches');
  const sw = switches.find(s => s.id === switchId);
  if (sw) {
    el('ports-modal-title').textContent = sw.name || sw.ip_address;
    el('ports-modal-sub').textContent   = sw.name ? sw.ip_address : '';
  }

  // reset add-ports form — start from next available port number
  await refreshPortsModal();
}

function closePortsModal() {
  el('ports-modal').classList.add('hidden');
  _portsModalSwitchId = null;
}

async function refreshPortsModal() {
  if (!_portsModalSwitchId) return;

  const [ports, devices] = await Promise.all([
    apiFetch(`/api/switches/${_portsModalSwitchId}/ports`),
    apiFetch('/api/devices'),
  ]);
  _portsModalDevices = devices;

  const tbody = el('ports-tbody');
  const empty = el('ports-empty');

  if (!ports.length) {
    tbody.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    tbody.innerHTML = ports.map(p => renderPortRow(p, devices)).join('');
  }

  // set default start port number for the add form
  const nextNum = ports.length ? Math.max(...ports.map(p => p.port_number)) + 1 : 1;
  el('ap-count').value = 1;
}

function renderPortRow(port, devices) {
  const typeOpts = ['RJ45', 'SFP+'].map(t =>
    `<option value="${t}" ${t === port.port_type ? 'selected' : ''}>${t}</option>`
  ).join('');

  const speedOpts = ['10M','100M','1G','2.5G','10G','25G','40G','100G'].map(s =>
    `<option value="${s}" ${s === port.speed ? 'selected' : ''}>${s}</option>`
  ).join('');

  const devOpts = `<option value="">— none —</option>` +
    devices.map(d => {
      const label = d.name || d.hostname || d.ip_address;
      const selected = d.id === port.device_id ? 'selected' : '';
      return `<option value="${d.id}" ${selected}>${esc(label)}</option>`;
    }).join('');

  return `<tr data-port-id="${port.id}">
    <td class="port-num">${port.port_number}</td>
    <td><input type="text" class="port-label-input" value="${esc(port.label || '')}"
      placeholder="label" onchange="savePortField(${port.id}, 'label', this.value)" /></td>
    <td><select class="port-select" onchange="savePortField(${port.id}, 'port_type', this.value)">${typeOpts}</select></td>
    <td><select class="port-select" onchange="savePortField(${port.id}, 'speed', this.value)">${speedOpts}</select></td>
    <td><select class="port-select port-device-select" onchange="savePortDevice(${port.id}, this)">${devOpts}</select></td>
    <td><button class="btn-delete" onclick="deletePort(${port.id})" title="Delete port">✕</button></td>
  </tr>`;
}

async function savePortField(portId, field, value) {
  try {
    await apiFetch(`/api/switches/${_portsModalSwitchId}/ports/${portId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [field]: value || null }),
    });
    await Promise.all([loadAllPorts(), loadSwitches()]);
    if (currentTab === 'topology') loadTopology();
    applyFilter();
  } catch (e) {
    console.error('savePortField:', e);
  }
}

async function savePortDevice(portId, selectEl) {
  const deviceId = selectEl.value ? parseInt(selectEl.value) : null;
  try {
    await apiFetch(`/api/switches/${_portsModalSwitchId}/ports/${portId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: deviceId }),
    });
    await Promise.all([loadAllPorts(), loadDevices(), loadSwitches()]);
    if (currentTab === 'topology') loadTopology();
    // re-render just the modal row without closing it
    await refreshPortsModal();
  } catch (e) {
    console.error('savePortDevice:', e);
    await refreshPortsModal();
  }
}

async function deletePort(portId) {
  try {
    await apiFetch(`/api/switches/${_portsModalSwitchId}/ports/${portId}`, { method: 'DELETE' });
    await Promise.all([loadAllPorts(), loadSwitches()]);
    await refreshPortsModal();
    if (currentTab === 'topology') loadTopology();
    applyFilter();
  } catch (e) { console.error('deletePort:', e); }
}

async function addPorts() {
  const count  = parseInt(el('ap-count').value) || 1;
  const type   = el('ap-type').value;
  const speed  = el('ap-speed').value;
  el('ap-error').textContent = '';

  if (count < 1 || count > 96) {
    el('ap-error').textContent = 'Count must be 1–96.';
    return;
  }

  // figure out next port number
  const existing = await apiFetch(`/api/switches/${_portsModalSwitchId}/ports`);
  let nextNum = existing.length ? Math.max(...existing.map(p => p.port_number)) + 1 : 1;

  for (let i = 0; i < count; i++) {
    await apiFetch(`/api/switches/${_portsModalSwitchId}/ports`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ port_number: nextNum + i, port_type: type, speed }),
    });
  }

  await Promise.all([loadAllPorts(), loadSwitches()]);
  await refreshPortsModal();
  if (currentTab === 'topology') loadTopology();
  applyFilter();
}

// ── topology graph ────────────────────────────────────────────────────────────

async function loadTopology() {
  try {
    const data = await apiFetch('/api/topology');
    renderTopology(data);
  } catch (e) { console.error('loadTopology:', e); }
}

function renderTopology(data) {
  const wrap  = el('topo-graph-wrap');
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

  const g = svg.append('g');
  svg.call(
    d3.zoom().scaleExtent([0.15, 4])
      .on('zoom', ev => g.attr('transform', ev.transform))
  );

  const nodes = data.nodes.map(n => ({ ...n }));
  const edges = data.edges.map(e => ({ ...e }));

  // assign mid-column offset so parallel edges between the same pair don't overlap
  const _pairCount = {}, _pairIdx = {};
  edges.forEach(e => {
    const k = [e.source, e.target].sort().join('|');
    _pairCount[k] = (_pairCount[k] || 0) + 1;
  });
  edges.forEach(e => {
    const k = [e.source, e.target].sort().join('|');
    const idx = (_pairIdx[k] = (_pairIdx[k] || 0) + 1);
    const total = _pairCount[k];
    e.midOffset = total === 1 ? 0 : (idx - (total + 1) / 2) * 18;
  });

  if (topoSimulation) topoSimulation.stop();
  topoSimulation = d3.forceSimulation(nodes)
    .force('link',      d3.forceLink(edges).id(d => d.id)
                          .distance(d => d.type === 'switch_link' ? 220 : 130))
    .force('charge',    d3.forceManyBody().strength(d => d.type === 'switch' ? -700 : -280))
    .force('center',    d3.forceCenter(W / 2, H / 2))
    .force('collision', d3.forceCollide(46));

  const edgeG = g.append('g').attr('class', 'topo-edges');
  const edge = edgeG.selectAll('path').data(edges).join('path')
    .attr('class', d => `topo-edge topo-edge-${d.type}`)
    .on('mouseenter', (ev, d) => showEdgeTip(ev, d))
    .on('mouseleave', hideEdgeTip);

  const nodeG = g.append('g').attr('class', 'topo-nodes');
  const node = nodeG.selectAll('g').data(nodes).join('g')
    .attr('class', d => {
      let cls = `topo-node topo-node-${d.type}`;
      if (d.type === 'device' && d.is_online === false) cls += ' topo-node-offline';
      return cls;
    })
    .call(d3.drag()
      .on('start', (ev, d) => { if (!ev.active) topoSimulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
      .on('end',   (ev, d) => { if (!ev.active) topoSimulation.alphaTarget(0); d.fx = null; d.fy = null; })
    )
    .on('click', (ev, d) => { ev.stopPropagation(); showNodeDetail(d); });

  node.filter(d => d.type === 'switch')
    .append('rect').attr('width', 52).attr('height', 34).attr('x', -26).attr('y', -17).attr('rx', 5);

  node.filter(d => d.type === 'device')
    .append('circle').attr('r', 20);

  node.append('text').attr('dy', d => d.type === 'switch' ? 28 : 33)
    .text(d => _truncate(d.label, 16));

  topoSimulation.on('tick', () => {
    edge.attr('d', d => {
      const sx = d.source.x, sy = d.source.y;
      const tx = d.target.x, ty = d.target.y;
      if (d.midOffset === 0) {
        const vx = (sx + tx) / 2;
        return `M${sx},${sy} H${vx} V${ty} H${tx}`;
      }
      // parallel edges: bezier with control point offset perpendicularly to the line
      // so curves separate regardless of whether nodes are horizontal or vertical
      const dx = tx - sx, dy = ty - sy;
      const len = Math.sqrt(dx * dx + dy * dy) || 1;
      const cx = (sx + tx) / 2 - (dy / len) * d.midOffset * 3;
      const cy = (sy + ty) / 2 + (dx / len) * d.midOffset * 3;
      return `M${sx},${sy} Q${cx},${cy} ${tx},${ty}`;
    });
    node.attr('transform', d => `translate(${d.x},${d.y})`);
  });

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

  const online = d.is_online === true  ? '<span style="color:var(--green)">Online</span>'
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
  const text = d.type === 'switch_link'
    ? `${d.port_a} (${d.port_a_type}·${d.speed_a}) ↔ ${d.port_b} (${d.port_b_type}·${d.speed_b})`
    : d.port ? `${d.port} · ${d.port_type} · ${d.speed}` : null;
  if (!text) return;
  if (!_edgeTip) {
    _edgeTip = document.createElement('div');
    _edgeTip.className = 'edge-tooltip';
    document.body.appendChild(_edgeTip);
  }
  _edgeTip.textContent = text;
  _edgeTip.style.left = (ev.pageX + 12) + 'px';
  _edgeTip.style.top  = (ev.pageY - 8)  + 'px';
  _edgeTip.style.display = 'block';
}

function hideEdgeTip() {
  if (_edgeTip) _edgeTip.style.display = 'none';
}

// ── helpers ───────────────────────────────────────────────────────────────────

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  if (res.status === 204) return null;
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

function _truncate(s, n) {
  return s && s.length > n ? s.slice(0, n) + '…' : s;
}
