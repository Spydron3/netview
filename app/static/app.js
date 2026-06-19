'use strict';

let allDevices = [];
let allPorts   = [];   // flat list from GET /api/ports — used for device dropdowns
let allRooms   = [];
let pollTimer  = null;
let currentTab = 'devices';
let _notifOpen = false;

// ── bootstrap ─────────────────────────────────────────────────────────────────

(async function init() {
  const savedOffline = localStorage.getItem('showOffline');
  if (savedOffline !== null) el('show-offline').checked = savedOffline === 'true';
  await Promise.all([loadRooms(), loadStats(), loadHistory(), loadAllPorts(), loadVersion(), loadNotifications()]);
  await loadDevices();
  startAutoRefresh();
  setInterval(loadNotifications, 30_000);
  document.addEventListener('click', e => {
    if (_notifOpen && !el('notif-wrapper').contains(e.target)) {
      el('notif-panel').classList.add('hidden');
      _notifOpen = false;
    }
  });
})();

async function loadVersion() {
  try {
    const data = await apiFetch('/api/version');
    el('version-badge').textContent = 'v' + data.version;
  } catch (_) {}
}

// ── tab switching ─────────────────────────────────────────────────────────────

function switchTab(name) {
  currentTab = name;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(tc => tc.classList.toggle('hidden', tc.id !== `tab-${name}`));
  if (name === 'topology') loadTopologyTab();
}

// ── settings ──────────────────────────────────────────────────────────────────

function switchSettingsTab(name) {
  document.querySelectorAll('.settings-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.settings-tab-content').forEach(c =>
    c.classList.toggle('hidden', c.dataset.tab !== name));
  const panel = document.querySelector('#settings-modal .modal-panel');
  if (panel) panel.classList.toggle('modal-panel-log', name === 'log');
  if (name === 'log') loadLog();
}

async function loadLog() {
  const viewer = el('log-viewer');
  if (!viewer) return;
  try {
    const data = await apiFetch('/api/log?lines=500');
    const html = (data.content || '').split('\n').map(line => {
      const safe = esc(line);
      if (/ ERROR /.test(line))   return `<span class="log-error">${safe}</span>`;
      if (/ WARNING /.test(line)) return `<span class="log-warning">${safe}</span>`;
      if (/ INFO /.test(line))    return `<span class="log-info">${safe}</span>`;
      return safe;
    }).join('\n');
    viewer.innerHTML = html || '<span style="color:var(--muted)">No log entries yet.</span>';
    viewer.scrollTop = viewer.scrollHeight;
  } catch (_) {
    viewer.textContent = 'Failed to load log.';
  }
}

function downloadLog() {
  window.open('/api/log/download', '_blank');
}

async function openSettings() {
  try {
    const s = await apiFetch('/api/settings');
    el('s-interval').value      = Math.round(parseInt(s.scan_interval || '300') / 60);
    el('s-network').value       = s.network_range || '';
    el('s-port-scan').checked   = (s.port_scan_enabled || 'true') === 'true';
    el('s-notify-new').checked  = s.notify_new_device === 'true';
    el('s-notify-ip').checked   = s.notify_ip_change  === 'true';
    el('s-smtp-host').value     = s.smtp_host || '';
    el('s-smtp-port').value     = s.smtp_port || '587';
    el('s-smtp-tls').checked    = (s.smtp_tls ?? 'true') === 'true';
    el('s-smtp-user').value     = s.smtp_user || '';
    el('s-smtp-pass').value     = s.smtp_password || '';
    el('s-smtp-from').value     = s.smtp_from || '';
    el('s-smtp-to').value       = s.smtp_to || '';
    el('settings-msg').textContent  = '';
    el('s-test-msg').textContent    = '';
    toggleSmtpFields();
  } catch (e) {
    console.error('loadSettings:', e);
  }
  el('settings-modal').classList.remove('hidden');
}

function toggleSmtpFields() {
  el('smtp-fields').style.display = (el('s-notify-new').checked || el('s-notify-ip').checked) ? 'block' : 'none';
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
        notify_new_device:  el('s-notify-new').checked,
        notify_ip_change:   el('s-notify-ip').checked,
        smtp_host:          el('s-smtp-host').value.trim(),
        smtp_port:          parseInt(el('s-smtp-port').value) || 587,
        smtp_tls:           el('s-smtp-tls').checked,
        smtp_user:          el('s-smtp-user').value.trim(),
        smtp_password:      el('s-smtp-pass').value,
        smtp_from:          el('s-smtp-from').value.trim(),
        smtp_to:            el('s-smtp-to').value.trim(),
      }),
    });
    showSettingsMsg('Saved.');
    setTimeout(closeSettings, 800);
  } catch (e) {
    showSettingsMsg('Failed to save.', true);
  }
}

async function sendTestEmail() {
  const msg = el('s-test-msg');
  msg.textContent = 'Sending…';
  msg.style.color = '';
  try {
    await apiFetch('/api/settings/test-email', { method: 'POST' });
    msg.textContent = 'Sent!';
    msg.style.color = 'var(--green)';
  } catch (e) {
    msg.textContent = e.message || 'Failed.';
    msg.style.color = 'var(--red)';
  }
}

async function runVendorLookupAll() {
  const btn = el('vendor-lookup-btn');
  const msg = el('vendor-lookup-msg');
  btn.disabled = true;
  msg.textContent = 'Running…';
  msg.style.color = '';
  try {
    await apiFetch('/api/vendor-lookup-all', { method: 'POST' });
    msg.textContent = 'Started — results will appear after the next page refresh.';
    msg.style.color = 'var(--green)';
  } catch (e) {
    msg.textContent = e.message || 'Failed.';
    msg.style.color = 'var(--red)';
  } finally {
    btn.disabled = false;
  }
}

function showSettingsMsg(text, error = false) {
  const msg = el('settings-msg');
  msg.textContent = text;
  msg.style.color = error ? 'var(--red)' : 'var(--green)';
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeSettings(); closePortsModal(); closeDevicePortsModal(); closeWlanModal(); }
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

async function loadRooms() {
  try {
    allRooms = await apiFetch('/api/rooms');
  } catch (e) {
    console.error('loadRooms:', e);
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
  localStorage.setItem('showOffline', showOffline);

  const filtered = allDevices.filter(d => {
    if (!showOffline && !d.is_online) return false;
    if (!q) return true;
    return (
      (d.ip_address  || '').includes(q) ||
      (d.name        || '').toLowerCase().includes(q) ||
      (d.hostname    || '').toLowerCase().includes(q) ||
      (d.mac_address || '').toLowerCase().includes(q) ||
      (d.vendor      || '').toLowerCase().includes(q) ||
      (d.room        || '').toLowerCase().includes(q)
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
        <span class="device-ip">${d.ip_address ? esc(d.ip_address) : '<em style="opacity:.5">no IP</em>'}</span>
      </div>
      <div class="device-name-row" onclick="startEditName(${d.id}, this)" title="Click to set a name">
        ${d.name
          ? `<span class="device-name">${esc(d.name)}</span>`
          : `<span class="device-name-placeholder">+ Add name</span>`}
      </div>
      ${d.hostname ? `<div class="device-hostname">${esc(d.hostname)}</div>` : ''}
      <div class="device-vendor-row" onclick="startEditVendor(${d.id}, this)" title="Click to edit vendor">
        ${d.vendor
          ? `<span class="device-vendor">${esc(d.vendor)}</span>`
          : `<span class="device-vendor-placeholder">+ Add vendor</span>`}
        ${!d.vendor && d.mac_address ? `<button class="btn-vendor-lookup" onclick="event.stopPropagation(); lookupVendor(${d.id})" title="Look up via macvendors.com">Lookup</button>` : ''}
      </div>
      ${d.mac_address ? `<div class="device-mac">${esc(d.mac_address)}</div>` : ''}
      ${(() => {
        const prev = (d.ip_history || []).filter(h => h.ip_address !== d.ip_address);
        return prev.length ? `<details class="ip-history">
          <summary>IP history (${prev.length} previous)</summary>
          ${prev.map(h => `<div class="ip-history-row">
            <span>${esc(h.ip_address)}</span>
            <span class="ip-history-date">${new Date(h.changed_at + 'Z').toLocaleString()}</span>
          </div>`).join('')}
        </details>` : '';
      })()}
      ${ports.length ? `
        <div class="ports">
          ${ports.map(p => d.ip_address
            ? `<a class="port-chip" href="https://${esc(d.ip_address)}:${p.port}" target="_blank" rel="noopener noreferrer" title="${esc(p.service || p.protocol)}">${p.port}</a>`
            : `<span class="port-chip">${p.port}</span>`).join('')}
          ${extra > 0 ? `<span class="port-more">+${extra} more</span>` : ''}
        </div>` : ''}
      ${!d.is_switch ? `<button class="btn btn-sm btn-ghost device-ports-btn" onclick="openDevicePortsModal(${d.id})">
        ${(d.switch_ports || []).length > 0
          ? `${d.switch_ports.length} port${d.switch_ports.length !== 1 ? 's' : ''}`
          : 'Assign port'}
      </button>` : ''}
      <div class="device-virtual-row">
        <label class="device-virtual-label">
          <input type="checkbox" ${d.is_wireless ? 'checked' : ''} onchange="toggleWireless(${d.id}, this.checked)" />
          Wireless
        </label>
        <label class="device-virtual-label">
          <input type="checkbox" ${d.is_virtual ? 'checked' : ''} onchange="toggleVirtual(${d.id}, this.checked)" />
          Virtual
        </label>
        <label class="device-virtual-label">
          <input type="checkbox" ${d.is_switch ? 'checked' : ''} onchange="toggleSwitch(${d.id}, this.checked)" />
          Switch
        </label>
        <label class="device-virtual-label">
          <input type="checkbox" ${d.is_access_point ? 'checked' : ''} onchange="toggleAccessPoint(${d.id}, this.checked)" />
          Access Point
        </label>
        ${d.is_virtual ? `
          <select class="virtual-parent-select" onchange="setVirtualParent(${d.id}, this.value)">
            <option value="">— no parent —</option>
            ${allDevices.filter(x => !x.is_virtual && x.id !== d.id).map(x =>
              `<option value="${x.id}" ${x.id === d.parent_id ? 'selected' : ''}>${esc(x.name || x.ip_address)}</option>`
            ).join('')}
          </select>
        ` : ''}
      </div>
      ${d.is_switch ? `
      <button class="btn btn-sm btn-ghost" onclick="openPortsModal(${d.id})" style="margin:4px 0 0">Manage Ports</button>
      ` : ''}
      ${d.is_access_point ? `
      <button class="btn btn-sm btn-ghost" onclick="openWlanModal(${d.id})" style="margin:4px 0 0">
        ${(d.wlans || []).length > 0 ? `${d.wlans.length} WLAN${d.wlans.length !== 1 ? 's' : ''}` : 'Manage WLANs'}
      </button>
      ` : ''}
      <div class="device-room-row">
        <select class="room-select" onchange="onRoomSelect(${d.id}, this)">
          <option value="">— no room —</option>
          ${allRooms.map(r => `<option value="${r.id}" ${r.id === d.room_id ? 'selected' : ''}>${esc(r.name)}</option>`).join('')}
          <option value="__new__">＋ New room…</option>
        </select>
        <input type="text" class="room-new-input" style="display:none"
          placeholder="Room name…" data-device-id="${d.id}"
          onkeydown="onNewRoomKey(${d.id}, this, event)"
          onblur="cancelNewRoom(this)" />
      </div>
      <div class="device-footer">
        <span>${d.is_wireless ? `<span class="device-wifi-badge">WiFi</span> ` : ''}${d.is_switch ? `<span class="device-switch-badge">SW</span> ` : ''}${d.is_access_point ? `<span class="device-ap-badge">AP</span> ` : ''}${d.is_virtual && d.parent_id ? `<span class="device-vm-badge">VM</span> ` : ''}${(() => { const ls = new Date(d.last_seen + 'Z'); const tip = fmt(ls); return d.is_online ? `Online · seen <span title="${tip}">${timeAgo(ls)}</span>` : `Offline · last seen <span title="${tip}">${timeAgo(ls)}</span>`; })()}</span>
        <button class="device-delete-btn" onclick="deleteDevice(${d.id})">Delete</button>
      </div>
    </div>`;
  }).join('');
}

// ── virtual device helpers ────────────────────────────────────────────────────

async function toggleWireless(deviceId, isWireless) {
  await apiFetch(`/api/devices/${deviceId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_wireless: isWireless }),
  });
  await loadDevices();
}

async function toggleVirtual(deviceId, isVirtual) {
  await apiFetch(`/api/devices/${deviceId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_virtual: isVirtual }),
  });
  await loadDevices();
}

async function toggleSwitch(deviceId, isSwitch) {
  await apiFetch(`/api/devices/${deviceId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_switch: isSwitch }),
  });
  await loadDevices();
  if (currentTab === 'topology') await loadTopologyTab();
}

async function deleteDevice(deviceId) {
  const dev = allDevices.find(d => d.id === deviceId);
  const label = dev ? (dev.name || dev.ip_address || dev.mac_address || 'Device ' + deviceId) : 'Device ' + deviceId;
  if (!confirm(`Delete "${label}"? This cannot be undone.`)) return;
  await apiFetch(`/api/devices/${deviceId}`, { method: 'DELETE' });
  await loadDevices();
  if (currentTab === 'topology') loadTopology();
}

async function toggleAccessPoint(deviceId, isAP) {
  await apiFetch(`/api/devices/${deviceId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_access_point: isAP }),
  });
  await loadDevices();
}

async function setVirtualParent(deviceId, parentIdStr) {
  const parent_id = parentIdStr ? parseInt(parentIdStr) : null;
  await apiFetch(`/api/devices/${deviceId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ parent_id }),
  });
  await loadDevices();
}

async function onRoomSelect(deviceId, sel) {
  if (sel.value === '__new__') {
    const input = sel.nextElementSibling;
    sel.style.display = 'none';
    input.style.display = '';
    input.focus();
    return;
  }
  await setDeviceRoomById(deviceId, sel.value ? parseInt(sel.value) : null);
}

async function onNewRoomKey(deviceId, input, event) {
  if (event.key === 'Escape') { cancelNewRoom(input); return; }
  if (event.key !== 'Enter') return;
  event.preventDefault();
  const name = input.value.trim();
  if (!name) { cancelNewRoom(input); return; }
  try {
    const room = await apiFetch('/api/rooms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    await loadRooms();
    await setDeviceRoomById(deviceId, room.id);
  } catch (e) {
    console.error('onNewRoomKey:', e);
    cancelNewRoom(input);
  }
}

function cancelNewRoom(input) {
  input.style.display = 'none';
  input.value = '';
  const sel = input.previousElementSibling;
  if (sel) {
    const devId = parseInt(input.dataset.deviceId);
    const dev = allDevices.find(d => d.id === devId);
    sel.value = dev?.room_id != null ? String(dev.room_id) : '';
    sel.style.display = '';
  }
}

async function setDeviceRoomById(deviceId, roomId) {
  try {
    await apiFetch(`/api/devices/${deviceId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room_id: roomId }),
    });
    const idx = allDevices.findIndex(d => d.id === deviceId);
    if (idx >= 0) {
      allDevices[idx].room_id = roomId;
      allDevices[idx].room = allRooms.find(r => r.id === roomId)?.name || null;
    }
    if (currentTab === 'topology') loadTopology();
    renderDevices(allDevices);
  } catch (e) { console.error('setDeviceRoomById:', e); }
}

// ── device ports modal ───────────────────────────────────────────────────────

let _devicePortsModalId = null;

async function openDevicePortsModal(deviceId) {
  _devicePortsModalId = deviceId;
  el('dp-error').textContent = '';

  const dev = allDevices.find(d => d.id === deviceId);
  el('device-ports-modal-title').textContent = dev ? (dev.name || dev.ip_address || 'Device') : 'Device';
  el('device-ports-modal-sub').textContent   = dev?.name ? (dev.ip_address || '') : '';

  el('device-ports-modal').classList.remove('hidden');
  await Promise.all([refreshDevicePortsModal(), _loadDPTargetDevices()]);
}

function closeDevicePortsModal() {
  el('device-ports-modal').classList.add('hidden');
  _devicePortsModalId = null;
}

async function refreshDevicePortsModal() {
  if (!_devicePortsModalId) return;
  const connections = await apiFetch(`/api/devices/${_devicePortsModalId}/connections`);

  const tbody = el('device-ports-tbody');
  const empty = el('device-ports-empty');
  if (!connections.length) {
    tbody.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    tbody.innerHTML = connections.map(c => {
      const portCell = c.port_label
        ? `${esc(c.port_label)}${c.port_type ? ` <span class="port-type-badge">${esc(c.port_type)}</span>` : ''}${c.speed ? ` ${esc(c.speed)}` : ''}`
        : '<span class="port-no-device">direct</span>';
      return `<tr>
        <td>${esc(c.other_device_label)}</td>
        <td>${portCell}</td>
        <td><button class="btn-delete" onclick="disconnectPort(${c.link_id})" title="Disconnect">✕</button></td>
      </tr>`;
    }).join('');
  }
}

async function _loadDPTargetDevices() {
  const devices = await apiFetch('/api/devices');
  el('dp-target-device').innerHTML = '<option value="">— select device —</option>' +
    devices
      .filter(d => d.id !== _devicePortsModalId && !d.is_virtual)
      .map(d => `<option value="${d.id}" data-is-switch="${d.is_switch}">${esc(d.name || d.ip_address || d.mac_address || 'Device ' + d.id)}</option>`)
      .join('');
  el('dp-target-port').style.display = 'none';
  el('dp-target-port').innerHTML = '<option value="">— select port —</option>';
}

async function loadDPTargetPorts() {
  const sel = el('dp-target-device');
  const deviceId = sel.value;
  const portSel = el('dp-target-port');
  if (!deviceId) { portSel.style.display = 'none'; return; }

  const isSwitch = sel.options[sel.selectedIndex]?.getAttribute('data-is-switch') === 'true';
  if (isSwitch) {
    const ports = await apiFetch(`/api/switches/${deviceId}/ports`);
    portSel.innerHTML = '<option value="">— select port —</option>' +
      ports.filter(p => !p.link_id).map(p => {
        const lbl = p.label || ('Port ' + p.port_number);
        return `<option value="${p.id}">${esc(lbl)} (${p.port_type}·${p.speed})</option>`;
      }).join('');
    portSel.style.display = '';
  } else {
    portSel.style.display = 'none';
  }
}

async function connectDP() {
  const sel = el('dp-target-device');
  const targetDeviceId = parseInt(sel.value);
  const isSwitch = sel.options[sel.selectedIndex]?.getAttribute('data-is-switch') === 'true';
  const targetPortId = isSwitch ? parseInt(el('dp-target-port').value) || null : null;
  el('dp-error').textContent = '';
  if (!targetDeviceId) { el('dp-error').textContent = 'Select a device.'; return; }
  if (isSwitch && !targetPortId) { el('dp-error').textContent = 'Select a port.'; return; }
  try {
    await apiFetch('/api/port-link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        device_a_id: _devicePortsModalId,
        device_b_id: targetDeviceId,
        switch_port_b_id: targetPortId || null,
      }),
    });
    el('dp-target-device').value = '';
    el('dp-target-port').style.display = 'none';
    await Promise.all([loadAllPorts(), refreshDevicePortsModal()]);
    applyFilter();
    if (currentTab === 'topology') loadTopology();
  } catch (e) {
    el('dp-error').textContent = e.message || 'Failed to connect.';
  }
}

// ── wlan modal ───────────────────────────────────────────────────────────────

let _wlanModalDeviceId = null;

async function openWlanModal(deviceId) {
  _wlanModalDeviceId = deviceId;
  const dev = allDevices.find(d => d.id === deviceId);
  el('wlan-modal-title').textContent = 'WLANs';
  el('wlan-modal-sub').textContent = dev ? (dev.name || dev.ip_address || 'Device ' + deviceId) : '';
  el('wlan-new-ssid').value = '';
  el('wlan-error').textContent = '';
  el('wlan-modal').classList.remove('hidden');
  await refreshWlanModal();
}

function closeWlanModal() {
  el('wlan-modal').classList.add('hidden');
  _wlanModalDeviceId = null;
}

async function refreshWlanModal() {
  if (!_wlanModalDeviceId) return;
  const wlans = await apiFetch(`/api/devices/${_wlanModalDeviceId}/wlans`);
  const tbody = el('wlan-tbody');
  el('wlan-empty').classList.toggle('hidden', wlans.length > 0);
  el('wlan-table').style.display = wlans.length ? '' : 'none';
  tbody.innerHTML = wlans.map(w => `
    <tr>
      <td>${esc(w.ssid)}</td>
      <td>${esc(w.band)} GHz</td>
      <td><button class="btn btn-sm btn-ghost btn-danger" onclick="deleteWlan(${w.id})">Delete</button></td>
    </tr>
  `).join('');
}

async function addWlan() {
  const ssid = el('wlan-new-ssid').value.trim();
  const band = el('wlan-new-band').value;
  el('wlan-error').textContent = '';
  if (!ssid) { el('wlan-error').textContent = 'SSID must not be empty.'; return; }
  try {
    const w = await apiFetch(`/api/devices/${_wlanModalDeviceId}/wlans`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ssid, band }),
    });
    el('wlan-new-ssid').value = '';
    const idx = allDevices.findIndex(d => d.id === _wlanModalDeviceId);
    if (idx >= 0) {
      allDevices[idx] = { ...allDevices[idx], wlans: [...(allDevices[idx].wlans || []), w] };
      applyFilter();
    }
    await refreshWlanModal();
  } catch (e) {
    el('wlan-error').textContent = e.message || 'Failed to add WLAN.';
  }
}

async function deleteWlan(wlanId) {
  await apiFetch(`/api/devices/${_wlanModalDeviceId}/wlans/${wlanId}`, { method: 'DELETE' });
  const idx = allDevices.findIndex(d => d.id === _wlanModalDeviceId);
  if (idx >= 0) {
    allDevices[idx] = { ...allDevices[idx], wlans: (allDevices[idx].wlans || []).filter(w => w.id !== wlanId) };
    applyFilter();
  }
  await refreshWlanModal();
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

function startEditVendor(deviceId, row) {
  if (row.querySelector('input')) return;
  const dev = allDevices.find(d => d.id === deviceId);
  const current = dev?.vendor || '';

  const input = document.createElement('input');
  input.className = 'name-input';
  input.value = current;
  input.placeholder = 'Vendor name…';
  row.innerHTML = '';
  row.appendChild(input);
  input.focus();
  input.select();

  let saved = false;

  async function save() {
    if (saved) return;
    saved = true;
    const vendor = input.value.trim() || null;
    try {
      const updated = await apiFetch(`/api/devices/${deviceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vendor }),
      });
      const idx = allDevices.findIndex(d => d.id === deviceId);
      if (idx >= 0) allDevices[idx] = updated;
    } catch (e) {
      console.error('Failed to save vendor:', e);
    }
    applyFilter();
  }

  input.addEventListener('blur', save);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { input.blur(); }
    if (e.key === 'Escape') { saved = true; applyFilter(); }
  });
}

async function lookupVendor(deviceId) {
  try {
    const updated = await apiFetch(`/api/devices/${deviceId}/vendor-lookup`, { method: 'POST' });
    const idx = allDevices.findIndex(d => d.id === deviceId);
    if (idx >= 0) allDevices[idx] = updated;
    applyFilter();
  } catch (e) {
    console.error('Vendor lookup failed:', e);
  }
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
      if (!isEditingName()) await loadDevices();
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
  el('scan-btn').disabled = running;
  el('stat-last-scan-card').classList.toggle('scanning', running);
}

// ── auto-refresh ──────────────────────────────────────────────────────────────

function isEditingName() {
  return !!document.querySelector('#device-grid .name-input');
}

function startAutoRefresh() {
  setInterval(async () => {
    const tasks = [loadStats(), loadAllPorts()];
    if (!isEditingName()) tasks.push(loadDevices());
    await Promise.all(tasks);
  }, 30_000);
}

// ── topology tab ──────────────────────────────────────────────────────────────

let topoSimulation = null;

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
  const ip   = el('sw-ip').value.trim()   || null;
  const mac  = el('sw-mac').value.trim()  || null;
  const name = el('sw-name').value.trim() || null;
  if (!ip && !mac && !name) { el('sw-form-error').textContent = 'At least one of IP, MAC, or name is required.'; return; }
  try {
    await apiFetch('/api/switches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip_address: ip, mac_address: mac, name: el('sw-name').value.trim() || null }),
    });
    hideAddSwitchForm();
    await Promise.all([loadSwitches(), loadDevices()]);
  } catch (e) {
    el('sw-form-error').textContent = e.message || 'Failed to add switch.';
  }
}

async function deleteSwitch(id) {
  if (!confirm('Remove this switch and all its ports?')) return;
  try {
    await apiFetch(`/api/switches/${id}`, { method: 'DELETE' });
    await Promise.all([loadSwitches(), loadAllPorts(), loadTopology()]);
    applyFilter();
  } catch (e) { console.error('deleteSwitch:', e); }
}

// ── port connect (from switch ports modal) ────────────────────────────────────

let _portConnectPortId = null;

async function showPortConnect(portId, portLabel) {
  _portConnectPortId = portId;
  el('pc-port-label-val').textContent = portLabel;
  el('pc-error').textContent = '';
  el('pc-dev-port').style.display = 'none';
  el('pc-dev-port').innerHTML = '<option value="">— select port —</option>';

  const devices = await apiFetch('/api/devices');
  el('pc-device').innerHTML = '<option value="">— select device —</option>' +
    devices
      .filter(d => d.id !== _portsModalSwitchId && !d.is_virtual)
      .map(d => `<option value="${d.id}" data-is-switch="${d.is_switch}">${esc(d.name || d.ip_address || d.mac_address || 'Device ' + d.id)}</option>`)
      .join('');

  el('port-connect-section').style.display = '';
}

function closePortConnect() {
  el('port-connect-section').style.display = 'none';
  _portConnectPortId = null;
}

async function loadPCDevicePorts() {
  const sel = el('pc-device');
  const deviceId = sel.value;
  const portSel = el('pc-dev-port');
  if (!deviceId) { portSel.style.display = 'none'; return; }

  const isSwitch = sel.options[sel.selectedIndex]?.getAttribute('data-is-switch') === 'true';
  if (isSwitch) {
    const ports = await apiFetch(`/api/switches/${deviceId}/ports`);
    portSel.innerHTML = '<option value="">— select port —</option>' +
      ports.filter(p => !p.link_id).map(p => {
        const lbl = p.label || ('Port ' + p.port_number);
        return `<option value="${p.id}">${esc(lbl)} (${p.port_type}·${p.speed})</option>`;
      }).join('');
    portSel.style.display = '';
  } else {
    portSel.style.display = 'none';
  }
}

async function savePortConnect() {
  const sel = el('pc-device');
  const targetDeviceId = parseInt(sel.value);
  const isSwitch = sel.options[sel.selectedIndex]?.getAttribute('data-is-switch') === 'true';
  const targetPortId = isSwitch ? parseInt(el('pc-dev-port').value) || null : null;
  el('pc-error').textContent = '';
  if (!targetDeviceId) { el('pc-error').textContent = 'Select a device.'; return; }
  if (isSwitch && !targetPortId) { el('pc-error').textContent = 'Select a port.'; return; }
  try {
    await apiFetch('/api/port-link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        device_a_id: _portsModalSwitchId,
        switch_port_a_id: _portConnectPortId,
        device_b_id: targetDeviceId,
        switch_port_b_id: targetPortId,
      }),
    });
    closePortConnect();
    await Promise.all([loadAllPorts(), loadSwitches(), refreshPortsModal()]);
    if (currentTab === 'topology') loadTopology();
    applyFilter();
  } catch (e) {
    el('pc-error').textContent = e.message || 'Failed to connect.';
  }
}

// ── unified disconnect ────────────────────────────────────────────────────────

async function disconnectPort(linkId) {
  try {
    await apiFetch(`/api/port-link/${linkId}`, { method: 'DELETE' });
    const tasks = [loadAllPorts(), loadSwitches()];
    if (!el('ports-modal').classList.contains('hidden') && _portsModalSwitchId)
      tasks.push(refreshPortsModal());
    if (!el('device-ports-modal').classList.contains('hidden') && _devicePortsModalId)
      tasks.push(refreshDevicePortsModal());
    await Promise.all(tasks);
    applyFilter();
    if (currentTab === 'topology') loadTopology();
  } catch (e) { console.error('disconnectPort:', e); }
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
    tbody.innerHTML = ports.map(p => renderPortRow(p)).join('');
  }

  // set default start port number for the add form
  const nextNum = ports.length ? Math.max(...ports.map(p => p.port_number)) + 1 : 1;
  el('ap-count').value = 1;
}

function renderPortRow(port) {
  const typeOpts = ['RJ45', 'SFP+'].map(t =>
    `<option value="${t}" ${t === port.port_type ? 'selected' : ''}>${t}</option>`
  ).join('');

  const speedOpts = ['10M','100M','1G','2.5G','10G','25G','40G','100G'].map(s =>
    `<option value="${s}" ${s === port.speed ? 'selected' : ''}>${s}</option>`
  ).join('');

  const portLabel = JSON.stringify(port.label || ('Port ' + port.port_number));
  const devCell = port.link_id
    ? `<span>${esc(port.device_label || '?')}</span> <button class="btn-xs" onclick="disconnectPort(${port.link_id})" title="Disconnect">✕</button>`
    : `<button class="btn-xs btn-ghost" onclick="showPortConnect(${port.id}, ${esc(portLabel)})">+ Connect</button>`;

  return `<tr data-port-id="${port.id}">
    <td class="port-num">${port.port_number}</td>
    <td><input type="text" class="port-label-input" value="${esc(port.label || '')}"
      placeholder="label" onchange="savePortField(${port.id}, 'label', this.value)" /></td>
    <td><select class="port-select" onchange="savePortField(${port.id}, 'port_type', this.value)">${typeOpts}</select></td>
    <td><select class="port-select" onchange="savePortField(${port.id}, 'speed', this.value)">${speedOpts}</select></td>
    <td class="port-device-cell">${devCell}</td>
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

function saveTopoPosition(nodeId, x, y) {
  apiFetch('/api/topology/positions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ node_id: nodeId, x: Math.round(x), y: Math.round(y) }),
  }).catch(e => console.error('saveTopoPosition:', e));
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

  // Pin all nodes immediately — saved positions or circular default around center
  const _newNodes = [];
  nodes.forEach((d, i) => {
    if (d.x !== undefined && d.y !== undefined) {
      d.fx = d.x;
      d.fy = d.y;
      d._pinned = true;
    } else {
      const angle = (i / Math.max(nodes.length, 1)) * 2 * Math.PI;
      d.x = W / 2 + 120 * Math.cos(angle);
      d.y = H / 2 + 120 * Math.sin(angle);
      d.fx = d.x;
      d.fy = d.y;
      d._pinned = true;
      _newNodes.push(d);
    }
  });
  // Save default positions for new nodes before simulation starts
  _newNodes.forEach(d => saveTopoPosition(d.id, d.x, d.y));

  if (topoSimulation) topoSimulation.stop();
  topoSimulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(edges).id(d => d.id).strength(0));

  // Room group inserted before edges/nodes so boxes render behind everything
  const roomG = g.insert('g', ':first-child').attr('class', 'topo-rooms');

  function updateRoomRects() {
    const roomMap = {};
    nodes.forEach(d => {
      if (!d.room) return;
      const hw = d.type === 'switch' ? 26 : ((d.virtual_children || []).length > 0 ? 75 : 20);
      const hh = d.type === 'switch' ? 17 : ((d.virtual_children || []).length > 0
        ? (6 + d.virtual_children.length * 12) : 20);
      const pad = 28;
      if (!roomMap[d.room]) roomMap[d.room] = { x1: Infinity, y1: Infinity, x2: -Infinity, y2: -Infinity };
      const r = roomMap[d.room];
      r.x1 = Math.min(r.x1, d.x - hw - pad);
      r.y1 = Math.min(r.y1, d.y - hh - pad);
      r.x2 = Math.max(r.x2, d.x + hw + pad);
      r.y2 = Math.max(r.y2, d.y + hh + pad);
    });
    const roomData = Object.entries(roomMap).map(([name, b]) => ({ name, ...b }));
    const sel = roomG.selectAll('g.room-box').data(roomData, d => d.name);
    const enter = sel.enter().append('g').attr('class', 'room-box');
    enter.append('rect').attr('rx', 10);
    enter.append('text').attr('class', 'room-label');
    const merged = sel.merge(enter);
    merged.select('rect')
      .attr('x', d => d.x1).attr('y', d => d.y1)
      .attr('width', d => Math.max(0, d.x2 - d.x1))
      .attr('height', d => Math.max(0, d.y2 - d.y1));
    merged.select('text')
      .attr('x', d => d.x1 + 10).attr('y', d => d.y1 + 16)
      .text(d => d.name);
    sel.exit().remove();
  }

  const edgeG = g.append('g').attr('class', 'topo-edges');
  const edge = edgeG.selectAll('path').data(edges).join('path')
    .attr('class', d => `topo-edge topo-edge-${d.type}`)
    .on('mouseenter', (ev, d) => showEdgeTip(ev, d))
    .on('mouseleave', hideEdgeTip);

  const nodeG = g.append('g').attr('class', 'topo-nodes');
  const node = nodeG.selectAll('g').data(nodes).join('g')
    .attr('class', d => {
      let cls = `topo-node topo-node-${d.type}`;
      if (d.is_online === false) cls += ' topo-node-offline';
      if (d.type === 'device' && (d.virtual_children || []).length > 0) cls += ' topo-node-vmhost';
      if ((d.type === 'device' && (d.is_wireless || d.is_access_point)) || (d.type === 'switch' && d.is_access_point)) cls += ' topo-node-wireless';
      return cls;
    })
    .call(d3.drag()
      .on('start', (ev, d) => { d.fx = d.x; d.fy = d.y; })
      .on('drag',  (ev, d) => { d.fx = ev.x; d.fy = ev.y; topoSimulation.alpha(0.1).restart(); })
      .on('end',   (ev, d) => {
        topoSimulation.alphaTarget(0);
        d._pinned = true;
        saveTopoPosition(d.id, d.x, d.y);
      })
    )
    .on('click', (ev, d) => { ev.stopPropagation(); showNodeDetail(d); });

  node.filter(d => d.type === 'switch')
    .append('rect').attr('width', 52).attr('height', 34).attr('x', -26).attr('y', -17).attr('rx', 5);
  node.filter(d => d.type === 'switch')
    .append('circle').attr('class', 'sw-status-dot').attr('r', 3.5).attr('cx', 20).attr('cy', -11);

  // Regular device nodes (no virtual children)
  node.filter(d => d.type === 'device' && !(d.virtual_children || []).length)
    .append('circle').attr('class', 'node-circle').attr('r', 20);

  // WLAN arcs: three ~90° arcs concentric around source (0,6), sweep=1 bows upward.
  // Endpoints computed at ±45° from vertical for each radius (4, 8, 12).
  const wifiNodes = node.filter(d =>
    (d.type === 'device' && (d.is_wireless || d.is_access_point) && !(d.virtual_children || []).length) ||
    (d.type === 'switch' && d.is_access_point)
  );
  wifiNodes.append('path')
    .attr('class', 'wifi-arc')
    .attr('d', 'M-8.5,-2.5 A12,12 0 0,1 8.5,-2.5 M-5.7,0.3 A8,8 0 0,1 5.7,0.3 M-2.8,3.2 A4,4 0 0,1 2.8,3.2');
  wifiNodes.append('circle')
    .attr('class', 'wifi-dot')
    .attr('cx', 0).attr('cy', 6).attr('r', 1.5);

  // VM host nodes: rounded rect with a vertical list of child VMs
  node.filter(d => d.type === 'device' && (d.virtual_children || []).length > 0)
    .each(function(d) {
      const n    = d.virtual_children.length;
      const rowH = 24;
      const w    = 150;
      const h    = 12 + n * rowH;
      const g2   = d3.select(this);
      g2.append('rect')
        .attr('width', w).attr('height', h)
        .attr('x', -w / 2).attr('y', -h / 2)
        .attr('rx', 6).attr('class', 'vmhost-rect');
      d.virtual_children.forEach((child, i) => {
        const cy = -h / 2 + 12 + i * rowH + rowH / 2 - 2;
        const cg = g2.append('g').attr('transform', `translate(0,${cy})`);
        cg.append('circle')
          .attr('cx', -w / 2 + 14).attr('r', 6)
          .attr('class', `vmchild-dot ${child.is_online ? 'vm-on' : 'vm-off'}`);
        cg.append('text')
          .attr('x', -w / 2 + 30).attr('dy', '0.35em')
          .attr('text-anchor', 'start')
          .attr('class', 'vmchild-label')
          .text(_truncate(child.label, 15));
      });
    });

  node.append('text').attr('dy', d => {
      if (d.type === 'switch') return 28;
      if ((d.virtual_children || []).length > 0) {
        const rowH = 24;
        const h = 12 + d.virtual_children.length * rowH;
        return h / 2 + 14;
      }
      return 33;
    })
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
    updateRoomRects();
  });

  svg.on('click', closeNodeDetail);
}

// ── node detail panel ─────────────────────────────────────────────────────────

async function showNodeDetail(d) {
  const panel = el('node-detail');
  const body  = el('node-detail-body');

  const rows = [];
  if (d.ip)       rows.push(['IP',       d.ip]);
  if (d.mac)      rows.push(['MAC',      d.mac]);
  if (d.hostname) rows.push(['Hostname', d.hostname]);
  if (d.vendor)   rows.push(['Vendor',   d.vendor]);
  if (d.room)     rows.push(['Room',     d.room]);
  if (d.name && d.name !== d.label) rows.push(['Name', d.name]);

  const online = d.is_online === true  ? '<span style="color:var(--green)">Online</span>'
               : d.is_online === false ? '<span style="color:var(--muted)">Offline</span>'
               : '';

  const vms = d.virtual_children || [];
  body.innerHTML = `
    <div class="nd-type">${d.type === 'switch' ? 'Switch' : 'Device'}</div>
    <div class="nd-label">${esc(d.label)}</div>
    ${online ? `<div class="nd-online">${online}</div>` : ''}
    <dl class="nd-props">
      ${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(String(v))}</dd>`).join('')}
    </dl>
    ${vms.length ? `
    <div class="nd-vms">
      <div class="nd-vms-title">Virtual machines (${vms.length})</div>
      ${vms.map(c => `
        <div class="nd-vm-row">
          <span class="nd-vm-dot ${c.is_online ? 'vm-on' : 'vm-off'}"></span>
          <span>${esc(c.label)}${c.ip ? ` <span class="nd-vm-ip">${esc(c.ip)}</span>` : ''}</span>
        </div>`).join('')}
    </div>` : ''}
    ${d.type === 'switch' ? '<div class="nd-ports-section" id="nd-ports"><span style="color:var(--muted);font-size:11px">Loading ports…</span></div>' : ''}`;

  panel.classList.remove('hidden');

  if (d.type === 'switch') {
    const swId = parseInt(d.id.slice(3));
    try {
      const ports = await apiFetch(`/api/switches/${swId}/ports`);
      const connected = ports.filter(p => p.link_id);
      const section = el('nd-ports');
      if (!ports.length) { section.innerHTML = ''; return; }
      section.innerHTML = `
        <div class="nd-ports-title">Ports (${connected.length}/${ports.length} connected)</div>
        <div class="nd-ports-list">
          ${ports.map(p => `
            <div class="nd-port-row ${p.link_id ? '' : 'nd-port-empty'}">
              <span class="nd-port-num">${p.label || 'Port ' + p.port_number}</span>
              <span class="nd-port-type">${p.port_type}·${p.speed}</span>
              ${p.link_id ? `<span class="nd-port-dev">${esc(p.device_label || '?')}</span>` : '<span class="nd-port-dev nd-port-empty">—</span>'}
            </div>`).join('')}
        </div>`;
    } catch (e) {
      const section = el('nd-ports');
      if (section) section.innerHTML = '';
    }
  }
}

function closeNodeDetail() {
  el('node-detail').classList.add('hidden');
}

// ── edge tooltip ──────────────────────────────────────────────────────────────

let _edgeTip = null;

function showEdgeTip(ev, d) {
  let text = null;
  if (d.type === 'switch_link') {
    text = (d.port_a && d.port_b)
      ? `${d.port_a} (${d.port_a_type}·${d.speed_a}) ↔ ${d.port_b} (${d.port_b_type}·${d.speed_b})`
      : (d.port_a || d.port)
        ? `${d.port_a || d.port} (${d.port_a_type || d.port_type}·${d.speed_a || d.speed})`
        : null;
  } else if (d.port) {
    text = `${d.port} · ${d.port_type} · ${d.speed}`;
  }
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

// ── notifications ─────────────────────────────────────────────────────────────

const _NOTIF_ICONS = {
  new_device: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>',
  ip_change:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 16V4m0 0L3 8m4-4 4 4M17 8v12m0 0 4-4m-4 4-4-4"/></svg>',
  error:      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
  warning:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
};

function _renderNotifications(items) {
  const list = el('notif-list');
  if (!items || !items.length) {
    list.innerHTML = '<div class="notif-empty">No notifications</div>';
    return;
  }
  list.innerHTML = items.map(n => `
    <div class="notif-item notif-${n.type}${n.read ? '' : ' notif-unread'}">
      <div class="notif-icon">${_NOTIF_ICONS[n.type] || _NOTIF_ICONS.warning}</div>
      <div class="notif-content">
        <div class="notif-title">${esc(n.title)}</div>
        ${n.body ? `<div class="notif-body">${esc(n.body)}</div>` : ''}
        <div class="notif-time">${new Date(n.created_at + 'Z').toLocaleString()}</div>
      </div>
    </div>`).join('');
}

async function loadNotifications() {
  try {
    const data = await apiFetch('/api/notifications');
    const badge = el('notif-badge');
    if (data.unread > 0) {
      badge.textContent = data.unread > 99 ? '99+' : data.unread;
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }
    if (_notifOpen) _renderNotifications(data.items);
    return data;
  } catch (_) {}
}

async function toggleNotifications() {
  const panel = el('notif-panel');
  _notifOpen = !_notifOpen;
  if (_notifOpen) {
    panel.classList.remove('hidden');
    const data = await loadNotifications();
    if (data) _renderNotifications(data.items);
  } else {
    panel.classList.add('hidden');
  }
}

async function markAllRead() {
  await apiFetch('/api/notifications/read-all', { method: 'POST' });
  const data = await apiFetch('/api/notifications');
  el('notif-badge').classList.add('hidden');
  _renderNotifications(data.items);
}

async function clearNotifications() {
  await apiFetch('/api/notifications', { method: 'DELETE' });
  el('notif-list').innerHTML = '<div class="notif-empty">No notifications</div>';
  el('notif-badge').classList.add('hidden');
}
