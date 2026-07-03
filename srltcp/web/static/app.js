let selectedPeer = null;
let myHashes = {};
let ws = null;
let peerTab = 'discovered';
let folderTarget = null;
let statusData = {};

function log(msg) {
  const el = document.getElementById('log');
  el.textContent = new Date().toLocaleTimeString() + ' ' + msg + '\n' + el.textContent.slice(0, 3000);
}

function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.onopen = () => {
    document.getElementById('ws-status').textContent = 'connected';
    document.getElementById('ws-status').classList.add('online');
  };
  ws.onclose = () => {
    document.getElementById('ws-status').textContent = 'disconnected';
    document.getElementById('ws-status').classList.remove('online');
    setTimeout(connectWs, 3000);
  };
  ws.onmessage = (ev) => {
    const { type, data } = JSON.parse(ev.data);
    if (type === 'status') renderStatus(data);
    else if (type === 'message') appendMessage(data);
    else if (type === 'peer_discovered') loadPeers();
    else if (type === 'link_up') { log('Link up: ' + data.name); loadPeers(); }
    else if (type === 'transfer_progress') renderTransfers(data);
    else if (type === 'transfer_complete') { log('Transfer complete: ' + data.filename); renderTransfers(); }
    else if (type === 'transport_event') log('Transport: ' + data.kind);
  };
}

function setPeerTab(tab) {
  peerTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  loadPeers();
}

async function loadPeers() {
  const [discRes, trustRes] = await Promise.all([
    fetch('/api/peers'),
    fetch('/api/trusted'),
  ]);
  const discovered = await discRes.json();
  const trusted = await trustRes.json();
  const el = document.getElementById('peers');

  if (peerTab === 'trusted') {
    el.innerHTML = trusted.map(p => `
      <div class="peer ${selectedPeer === p.hash_id ? 'selected' : ''}" onclick="selectPeer('${p.hash_id}', '${escapeHtml(p.name)}', '${p.transport}')">
        <div class="peer-name">${escapeHtml(p.name)}</div>
        <div class="peer-meta">${p.hash_id.slice(0,12)}… · ${p.transport} · trusted</div>
        <div class="peer-actions">
          <button class="secondary" onclick="event.stopPropagation();pingPeer('${p.hash_id}')">Ping</button>
          <button class="danger" onclick="event.stopPropagation();untrustPeer('${p.hash_id}')">Remove</button>
        </div>
      </div>
    `).join('') || '<div class="empty">No trusted peers — trust someone from Discovered</div>';
    return;
  }

  const trustedIds = new Set(trusted.map(p => p.hash_id));
  el.innerHTML = discovered.map(p => {
    const metrics = [];
    if (p.rtt_ms != null) metrics.push(`${Math.round(p.rtt_ms)} ms`);
    if (p.transport === 'serial' && p.link_quality_pct != null) metrics.push(`${p.link_quality_pct}% RF`);
    const m = metrics.length ? ` · <span class="metrics">${metrics.join(' · ')}</span>` : '';
    const isTrusted = trustedIds.has(p.hash_id);
    return `
      <div class="peer ${selectedPeer === p.hash_id ? 'selected' : ''}" onclick="selectPeer('${p.hash_id}', '${escapeHtml(p.name)}', '${p.transport}')">
        <div class="peer-name">${escapeHtml(p.name)}</div>
        <div class="peer-meta">${p.hash_id.slice(0,12)}… · ${p.transport}${m}</div>
        <div class="peer-actions">
          ${isTrusted ? '' : `<button class="secondary" onclick="event.stopPropagation();trustPeer('${p.hash_id}')">Trust</button>`}
          <button class="secondary" onclick="event.stopPropagation();pingPeer('${p.hash_id}')">Ping</button>
        </div>
      </div>
    `;
  }).join('') || '<div class="empty">No peers discovered — others will appear when they announce</div>';
}

async function trustPeer(hashId) {
  await fetch('/api/trusted', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ hash_id: hashId }),
  });
  log('Trusted peer ' + hashId.slice(0, 12));
  loadPeers();
}

async function untrustPeer(hashId) {
  await fetch('/api/trusted/' + encodeURIComponent(hashId), { method: 'DELETE' });
  if (selectedPeer === hashId) {
    selectedPeer = null;
    document.getElementById('msg-input').disabled = true;
    document.getElementById('send-btn').disabled = true;
    document.getElementById('file-btn').disabled = true;
  }
  loadPeers();
}

async function pingPeer(hashId) {
  const res = await fetch('/api/ping', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ hash_id: hashId }),
  });
  const data = await res.json();
  const parts = [];
  if (data.rtt_ms != null) parts.push(Math.round(data.rtt_ms) + ' ms');
  if (data.link_quality_pct != null) parts.push(data.link_quality_pct + '% RF');
  log('Ping ' + hashId.slice(0, 12) + ': ' + (parts.join(', ') || 'sent'));
  loadPeers();
}

function renderStatus(data) {
  statusData = data;
  const version = data.version || '0.1.2';
  document.getElementById('version-badge').textContent = 'v' + version;

  const ids = data.identities || {};
  myHashes = {};
  document.getElementById('identities').innerHTML = Object.entries(ids).map(([t, id]) => {
    myHashes[t] = id.hash_id;
    return `<div class="identity-card">
      <strong>${t.toUpperCase()}</strong> — ${escapeHtml(id.name)}<br>
      <code>${id.hash_id}</code>
      <div class="identity-actions">
        <button class="secondary" onclick="regenerateIdentity('${t}')">Regenerate</button>
        <button class="danger" onclick="deleteIdentity('${t}')">Delete</button>
        <button class="secondary" onclick="createIdentity('${t}')">Create</button>
      </div>
    </div>`;
  }).join('') || '<div class="empty">No identities</div>';

  if (data.settings) populateSettingsForm(data.settings);
  loadPeers();
  renderTransfers();
}

async function renderTransfers(latest) {
  let transfers = latest ? [latest] : null;
  if (!transfers) {
    const res = await fetch('/api/transfers');
    transfers = await res.json();
  }
  const el = document.getElementById('transfers');
  el.innerHTML = transfers.map(t => {
    const pct = t.size ? Math.round((t.offset / t.size) * 100) : 0;
    const speed = t.speed_mbps ? t.speed_mbps.toFixed(2) + ' MB/s' : '';
    return `<div class="transfer-item">
      <div>${escapeHtml(t.filename)} <span style="color:var(--muted)">${t.state}</span></div>
      <div class="progress-bar"><div style="width:${pct}%"></div></div>
      <div class="transfer-speed">${pct}% · ${formatBytes(t.offset)} / ${formatBytes(t.size)} ${speed}</div>
    </div>`;
  }).join('') || '<div class="empty">No active transfers</div>';
}

function formatBytes(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}

function selectPeer(hashId, name, transport) {
  selectedPeer = hashId;
  document.getElementById('chat-title').textContent = 'Chat · ' + name;
  document.getElementById('msg-input').disabled = false;
  document.getElementById('send-btn').disabled = false;
  document.getElementById('file-btn').disabled = false;
  document.getElementById('peer-transport').value = transport || 'tcp';
  loadPeers();
  loadMessages();
  connectPeer(hashId, transport);
}

async function connectPeer(hashId, transport) {
  await fetch('/api/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ hash_id: hashId, transport: transport || 'tcp' }),
  });
  log('Connecting to ' + hashId.slice(0, 12) + '…');
}

async function loadMessages() {
  const res = await fetch('/api/messages');
  const msgs = await res.json();
  const el = document.getElementById('messages');
  const filtered = selectedPeer
    ? msgs.filter(m => m.sender_hash === selectedPeer || m.recipient_hash === selectedPeer)
    : msgs;
  el.innerHTML = filtered.map(m => {
    const out = Object.values(myHashes).includes(m.sender_hash);
    return `<div class="msg ${out ? 'out' : 'in'}">${escapeHtml(m.text)}<div class="msg-time">${m.status}</div></div>`;
  }).join('');
  el.scrollTop = el.scrollHeight;
}

function appendMessage(m) {
  if (selectedPeer && m.sender_hash !== selectedPeer && m.recipient_hash !== selectedPeer) return;
  const el = document.getElementById('messages');
  const out = Object.values(myHashes).includes(m.sender_hash);
  el.innerHTML += `<div class="msg ${out ? 'out' : 'in'}">${escapeHtml(m.text)}<div class="msg-time">${m.status}</div></div>`;
  el.scrollTop = el.scrollHeight;
}

async function sendMessage() {
  const input = document.getElementById('msg-input');
  const text = input.value.trim();
  if (!text || !selectedPeer) return;
  const transport = document.getElementById('peer-transport').value;
  const res = await fetch('/api/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ recipient_hash: selectedPeer, text, transport }),
  });
  if (!res.ok) {
    const err = await res.json();
    log('Send failed: ' + (err.error || res.status));
    return;
  }
  input.value = '';
}

async function sendFile() {
  const fileInput = document.getElementById('file-input');
  if (!selectedPeer || !fileInput.files.length) return;
  const file = fileInput.files[0];
  log('Uploading ' + file.name + '…');
  const form = new FormData();
  form.append('file', file);
  const upRes = await fetch('/api/upload', { method: 'POST', body: form });
  if (!upRes.ok) { log('Upload failed'); return; }
  const uploaded = await upRes.json();
  const transport = document.getElementById('peer-transport').value;
  const res = await fetch('/api/transfer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ recipient_hash: selectedPeer, path: uploaded.path, transport }),
  });
  if (!res.ok) {
    const err = await res.json();
    log('Transfer failed: ' + (err.error || res.status));
    return;
  }
  log('Transfer started: ' + file.name);
  fileInput.value = '';
  renderTransfers();
}

async function announce() {
  await fetch('/api/announce', { method: 'POST' });
  log('Announced on all transports');
}

function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

function populateSettingsForm(s) {
  document.getElementById('set-name').value = s.display_name || '';
  document.getElementById('set-tcp-port').value = s.tcp_port || 7825;
  document.getElementById('set-web-port').value = s.web_port || 8743;
  document.getElementById('set-auto-announce').checked = !!s.auto_announce;
  document.getElementById('set-enable-serial').checked = !!s.enable_serial;
  document.getElementById('set-serial-port').value = s.serial_port || '';
  document.getElementById('set-serial-baud').value = s.serial_baud || 115200;
  document.getElementById('set-incoming').value = s.incoming_files_dir || '';
  document.getElementById('set-shared').value = s.shared_folder || '';
  document.getElementById('set-retention').value = s.message_retention_preset || '1w';
  document.getElementById('set-relay').checked = !!s.relay_mode;
}

async function saveSettings() {
  const body = {
    display_name: document.getElementById('set-name').value,
    tcp_port: parseInt(document.getElementById('set-tcp-port').value, 10),
    web_port: parseInt(document.getElementById('set-web-port').value, 10),
    auto_announce: document.getElementById('set-auto-announce').checked,
    enable_serial: document.getElementById('set-enable-serial').checked,
    serial_port: document.getElementById('set-serial-port').value,
    serial_baud: parseInt(document.getElementById('set-serial-baud').value, 10),
    incoming_files_dir: document.getElementById('set-incoming').value,
    shared_folder: document.getElementById('set-shared').value,
    message_retention_preset: document.getElementById('set-retention').value,
    relay_mode: document.getElementById('set-relay').checked,
  };
  await fetch('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  log('Settings saved');
  closeModal('settings-modal');
  const res = await fetch('/api/status');
  renderStatus(await res.json());
}

async function restartApp() {
  if (!confirm('Restart SRLTCP? The page will reload.')) return;
  await fetch('/api/restart', { method: 'POST' });
  log('Restarting…');
  setTimeout(() => location.reload(), 3000);
}

async function showReleaseNotes() {
  const res = await fetch('/api/release-notes');
  const data = await res.json();
  document.getElementById('release-notes-body').textContent = data.notes;
  openModal('release-modal');
}

function openFolderPicker(targetId) {
  folderTarget = targetId;
  browseFolder(null);
  openModal('folder-modal');
}

async function browseFolder(path) {
  const url = path ? '/api/browse?path=' + encodeURIComponent(path) : '/api/browse';
  const res = await fetch(url);
  const data = await res.json();
  document.getElementById('folder-crumb').textContent = data.path;
  const el = document.getElementById('folder-list');
  let html = '';
  if (data.parent && data.parent !== data.path) {
    html += `<div class="folder-entry dir" onclick="browseFolder('${escapeHtml(data.parent)}')">..</div>`;
  }
  html += data.entries.filter(e => e.type === 'dir').map(e =>
    `<div class="folder-entry dir" onclick="browseFolder('${escapeHtml(e.path)}')">${escapeHtml(e.name)}</div>`
  ).join('');
  el.innerHTML = html || '<div class="empty">No folders</div>';
}

function selectFolder() {
  const path = document.getElementById('folder-crumb').textContent;
  if (folderTarget) document.getElementById(folderTarget).value = path;
  closeModal('folder-modal');
}

async function regenerateIdentity(transport) {
  if (!confirm('Regenerate ' + transport + ' identity? Old hash will no longer work.')) return;
  await fetch('/api/identities/' + transport + '/regenerate', { method: 'POST' });
  log('Regenerated ' + transport + ' identity');
  const res = await fetch('/api/status');
  renderStatus(await res.json());
}

async function deleteIdentity(transport) {
  if (!confirm('Delete ' + transport + ' identity?')) return;
  await fetch('/api/identities/' + transport, { method: 'DELETE' });
  log('Deleted ' + transport + ' identity');
  const res = await fetch('/api/status');
  renderStatus(await res.json());
}

async function createIdentity(transport) {
  await fetch('/api/identities/' + transport, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transport }),
  });
  log('Created ' + transport + ' identity');
  const res = await fetch('/api/status');
  renderStatus(await res.json());
}

document.getElementById('msg-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') sendMessage();
});

connectWs();
fetch('/api/status').then(r => r.json()).then(renderStatus);