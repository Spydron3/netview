'use strict';

let allDevices = [];
let pollTimer = null;

// ── bootstrap ─────────────────────────────────────────────────────────────────

(async function init() {
  await Promise.all([loadStats(), loadDevices(), loadHistory()]);
  startAutoRefresh();
})();

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
