/* SRLTCP Chat UI */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const state = {
    selectedPeer: null,
    selectedName: null,
    myHashes: {},
    myName: "You",
    peers: [],
    trusted: [],
    peerTab: "discovered",
    links: {},
    linkMetrics: {},
    folderTarget: null,
    ws: null,
    search: "",
    settings: {},
    interfaces: [],
    transfers: {},
    messageCache: [],
    settingsFormDirty: false,
    interfacesLoaded: false,
    timezones: [],
    contactMenuTarget: null,
    connectPending: new Map(),
  };

  const COLORS = [
    "#5b8def", "#3ecf8e", "#f5a623", "#f07178",
    "#c678dd", "#56b6c2", "#e5c07b", "#61afef",
  ];

  function hashColor(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) h = str.charCodeAt(i) + ((h << 5) - h);
    return COLORS[Math.abs(h) % COLORS.length];
  }

  function initials(name) {
    return (name || "?")
      .split(/[\s._-]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((w) => w[0].toUpperCase())
      .join("") || "?";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function formatDate(ts) {
    const d = new Date(ts * 1000);
    const today = new Date();
    if (d.toDateString() === today.toDateString()) return "Today";
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
    return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
  }

  function toast(msg) {
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), 3200);
  }

  function logActivity(msg) {
    const log = $("#activity-log");
    const entry = document.createElement("div");
    entry.className = "entry";
    entry.textContent = `${new Date().toLocaleTimeString()} ${msg}`;
    log.prepend(entry);
    while (log.children.length > 40) log.lastChild.remove();
  }

  function setAvatar(el, name, hashId) {
    el.textContent = initials(name);
    el.style.background = `${hashColor(hashId || name)}22`;
    el.style.color = hashColor(hashId || name);
    el.style.borderColor = `${hashColor(hashId || name)}44`;
  }

  function isOutgoing(senderHash) {
    return Object.values(state.myHashes).includes(senderHash);
  }

  function peerByHash(hashId) {
    return state.trusted.find((p) => p.hash_id === hashId)
      || state.peers.find((p) => p.hash_id === hashId);
  }

  function peerTransport(hashId) {
    return peerByHash(hashId)?.transport || "tcp";
  }

  function isPeerLinked(hashId) {
    if (state.links[hashId]) return true;
    const link = (state._lastLinks || []).find((l) => l.hash_id === hashId);
    return !!(link && link.handshake_complete);
  }

  function syncLinksFromStatus(links) {
    state._lastLinks = links || [];
    (links || []).forEach((l) => {
      state.links[l.hash_id] = l.handshake_complete;
      if (l.rtt_ms != null) {
        state.linkMetrics[l.hash_id] = {
          rtt_ms: l.rtt_ms,
          link_quality_pct: l.link_quality_pct,
        };
      }
    });
  }

  function formatBytes(n) {
    if (!n) return "0 B";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  /* ── WebSocket ── */
  function connectWs() {
    const host = location.host || "127.0.0.1:9876";
    state.ws = new WebSocket(`wss://${host}/ws`);

    state.ws.onopen = () => {
      $("#connection-status").textContent = "Connected";
      $("#connection-status").classList.add("online");
    };

    state.ws.onclose = () => {
      $("#connection-status").textContent = "Reconnecting…";
      $("#connection-status").classList.remove("online");
      setTimeout(connectWs, 3000);
    };

    state.ws.onmessage = (ev) => {
      const { type, data } = JSON.parse(ev.data);
      switch (type) {
        case "status":
          renderStatus(data);
          break;
        case "message":
          onNewMessage(data);
          break;
        case "peer_discovered":
          loadPeers();
          renderContacts();
          break;
        case "link_up":
          state.links[data.hash_id] = true;
          loadPeers();
          if (state.selectedPeer === data.hash_id) {
            refreshPeerStatus(data.hash_id);
            $("#msg-input").disabled = false;
            $("#send-btn").disabled = false;
          }
          toast(`Connected to ${data.name || data.hash_id?.slice(0, 8)}`);
          logActivity(`Link up: ${data.name || data.hash_id?.slice(0, 8)}`);
          break;
        case "link_down":
          delete state.links[data.hash_id];
          delete state.linkMetrics[data.hash_id];
          loadPeers();
          if (state.selectedPeer === data.hash_id) {
            refreshPeerStatus(data.hash_id);
          }
          toast(`Disconnected from ${data.name || data.hash_id?.slice(0, 8)}`);
          logActivity(`Link down: ${data.name || data.hash_id?.slice(0, 8)}`);
          break;
        case "peer_metrics":
          state.linkMetrics[data.hash_id] = {
            rtt_ms: data.rtt_ms,
            link_quality_pct: data.link_quality_pct,
          };
          loadPeers();
          if (state.selectedPeer === data.hash_id) refreshPeerStatus(data.hash_id);
          break;
        case "transfer_progress":
        case "transfer_complete":
          state.transfers[data.id] = data;
          updateTransferDock(data);
          renderTransfers();
          if (state.selectedPeer) updateChatTransfer(data);
          if (type === "transfer_complete") {
            toast(`Transfer complete: ${data.filename}`);
            hideTransferDockIfDone(data.id);
          }
          break;
        case "transport_event":
          logActivity(`Transport: ${data.kind}${data.hash_id ? ` (${data.hash_id.slice(0, 8)})` : ""}`);
          if (data.kind === "disconnected" && data.hash_id) {
            delete state.links[data.hash_id];
            delete state.linkMetrics[data.hash_id];
            if (state.selectedPeer === data.hash_id) refreshPeerStatus(data.hash_id);
            loadPeers();
          }
          break;
      }
    };
  }

  /* ── API ── */
  async function loadPeers() {
    const [pRes, tRes] = await Promise.all([
      fetch("/api/peers"),
      fetch("/api/trusted"),
    ]);
    state.peers = await pRes.json();
    state.trusted = await tRes.json();
    renderContacts();
  }

  async function trustPeer(hashId) {
    const discovered = state.peers.find((p) => p.hash_id === hashId);
    await fetch("/api/trusted", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        hash_id: hashId,
        transport: discovered?.transport || "tcp",
      }),
    });
    toast("Peer trusted");
    state.peers = state.peers.filter((p) => p.hash_id !== hashId);
    loadPeers();
  }

  async function deleteTrusted(hashId, name) {
    if (!confirm(`Remove ${name} from trusted contacts?`)) return;
    closeContactMenu();
    state.trusted = state.trusted.filter((p) => p.hash_id !== hashId);
    delete state.links[hashId];
    delete state.linkMetrics[hashId];
    if (state.selectedPeer === hashId) {
      state.selectedPeer = null;
      $("#chat-active").classList.add("hidden");
      $("#chat-empty").classList.remove("hidden");
    }
    renderContacts();
    const res = await fetch(`/api/trusted/${encodeURIComponent(hashId)}`, { method: "DELETE" });
    if (!res.ok) {
      toast("Failed to remove contact");
      loadPeers();
      return;
    }
    toast("Contact removed");
    loadPeers();
  }

  async function clearChatHistory(hashId) {
    closeContactMenu();
    const res = await fetch(`/api/trusted/${encodeURIComponent(hashId)}/clear-chat`, {
      method: "POST",
    });
    if (!res.ok) {
      toast("Failed to clear chat");
      return;
    }
    const data = await res.json();
    toast(`Cleared ${data.cleared || 0} message(s)`);
    if (state.selectedPeer === hashId) loadMessages();
  }

  async function renameContact(hashId, currentName) {
    closeContactMenu();
    const name = prompt("Rename contact:", currentName);
    if (!name || name.trim() === currentName) return;
    const res = await fetch(`/api/trusted/${encodeURIComponent(hashId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    if (!res.ok) {
      toast("Rename failed");
      return;
    }
    toast("Contact renamed");
    if (state.selectedPeer === hashId) {
      state.selectedName = name.trim();
      $("#chat-peer-name").textContent = name.trim();
    }
    loadPeers();
  }

  async function blockContact(hashId, name) {
    closeContactMenu();
    if (!confirm(`Block ${name}? They cannot message you until unblocked.`)) return;
    const res = await fetch(`/api/trusted/${encodeURIComponent(hashId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ blocked: true }),
    });
    if (!res.ok) {
      toast("Block failed");
      return;
    }
    delete state.links[hashId];
    delete state.linkMetrics[hashId];
    toast(`${name} blocked`);
    loadPeers();
  }

  function applyClockVisibility() {
    const show = state.settings.show_clock !== false;
    $("#header-clock-row")?.classList.toggle("hidden", !show);
  }

  function closeContactMenu() {
    const menu = $("#contact-menu");
    if (!menu) return;
    menu.classList.add("hidden");
    menu.setAttribute("aria-hidden", "true");
    state.contactMenuTarget = null;
  }

  function openContactMenu(hashId, name, anchor) {
    const menu = $("#contact-menu");
    if (!menu) return;
    state.contactMenuTarget = { hashId, name };
    const rect = anchor.getBoundingClientRect();
    menu.style.top = `${Math.min(rect.bottom + 4, window.innerHeight - 180)}px`;
    menu.style.left = `${Math.max(8, rect.left - 140)}px`;
    menu.classList.remove("hidden");
    menu.setAttribute("aria-hidden", "false");
    const peer = state.trusted.find((p) => p.hash_id === hashId);
    const blockBtn = menu.querySelector('[data-action="block"]');
    if (blockBtn) {
      blockBtn.textContent = peer?.blocked ? "Unblock contact" : "Block contact";
    }
  }

  async function pingPeer(hashId) {
    const res = await fetch("/api/ping", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hash_id: hashId }),
    });
    const data = await res.json();
    const parts = [];
    if (data.rtt_ms != null) parts.push(`${Math.round(data.rtt_ms)} ms`);
    if (data.link_quality_pct != null) parts.push(`${data.link_quality_pct}% RF`);
    toast(parts.length ? `Ping: ${parts.join(" · ")}` : "Ping sent");
    loadPeers();
  }

  async function loadMessages() {
    if (!state.selectedPeer) return;
    const res = await fetch("/api/messages?limit=500");
    const msgs = await res.json();
    const filtered = msgs.filter(
      (m) => m.sender_hash === state.selectedPeer || m.recipient_hash === state.selectedPeer
    );
    renderMessages(filtered);
  }

  async function announceTransport(transport) {
    const res = await fetch(`/api/announce?transport=${transport}`, { method: "POST" });
    if (!res.ok) {
      toast(`Announce ${transport.toUpperCase()} failed`);
      return;
    }
    toast(`Announced on ${transport.toUpperCase()}`);
    logActivity(`Announced on ${transport}`);
    setTimeout(loadPeers, 800);
  }

  function formatLatency(hashId) {
    const m = state.linkMetrics[hashId] || {};
    const link = (state._lastLinks || []).find((l) => l.hash_id === hashId);
    const rtt = m.rtt_ms ?? link?.rtt_ms;
    if (rtt != null) return `${Math.round(rtt)} ms`;
    return null;
  }

  function refreshPeerStatus(hashId) {
    const linked = isPeerLinked(hashId);
    const latency = formatLatency(hashId);
    if (linked && latency) {
      updatePeerStatus(`Encrypted · Online · ${latency}`);
      $(".status-dot").className = "status-dot online";
    } else if (linked) {
      updatePeerStatus("Encrypted · Online");
      $(".status-dot").className = "status-dot online";
    } else {
      updatePeerStatus("Handshaking…");
      $(".status-dot").className = "status-dot pending";
    }
    $("#msg-input").disabled = !hashId;
    $("#send-btn").disabled = !hashId;
  }

  async function connectPeer(hashId, force = false) {
    if (!hashId) return;
    if (isPeerLinked(hashId) && !force) {
      refreshPeerStatus(hashId);
      return;
    }
    if (state.connectPending.has(hashId)) {
      return state.connectPending.get(hashId);
    }
    const run = (async () => {
      if (state.selectedPeer === hashId) {
        updatePeerStatus("Handshaking…");
        $(".status-dot").className = "status-dot pending";
      }
      logActivity(`Connecting to ${hashId.slice(0, 12)}…`);
      const transport = peerTransport(hashId);
      const res = await fetch("/api/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hash_id: hashId, transport, force }),
      });
      const data = await res.json().catch(() => ({}));
      if (data.handshake_complete) {
        state.links[hashId] = true;
        if (data.rtt_ms != null) {
          state.linkMetrics[hashId] = { rtt_ms: data.rtt_ms };
        }
        if (state.selectedPeer === hashId) refreshPeerStatus(hashId);
      } else if (data.connected) {
        if (state.selectedPeer === hashId) {
          updatePeerStatus("Handshaking…");
          $(".status-dot").className = "status-dot pending";
        }
      } else if (state.selectedPeer === hashId) {
        updatePeerStatus("Connection failed");
        toast("Could not connect to peer");
      }
      return data;
    })();
    state.connectPending.set(hashId, run);
    try {
      return await run;
    } finally {
      state.connectPending.delete(hashId);
    }
  }

  async function disconnectPeer(hashId) {
    await fetch("/api/disconnect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hash_id: hashId }),
    });
    delete state.links[hashId];
    delete state.linkMetrics[hashId];
    updatePeerStatus("Disconnected");
    toast("Disconnected");
    loadPeers();
  }

  async function sendMessage() {
    const input = $("#msg-input");
    const text = input.value.trim();
    if (!text || !state.selectedPeer) return;

    input.value = "";
    autoResize(input);

    const res = await fetch("/api/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recipient_hash: state.selectedPeer,
        text,
        transport: peerTransport(state.selectedPeer),
      }),
    });
    if (!res.ok) {
      toast("Message failed — reconnecting…");
      await connectPeer(state.selectedPeer, true);
    } else {
      loadMessages();
    }
  }

  async function sendFile(file) {
    if (!state.selectedPeer || !file) return;
    if (!isPeerLinked(state.selectedPeer)) {
      toast("Not connected — handshaking…");
      await connectPeer(state.selectedPeer, true);
      if (!isPeerLinked(state.selectedPeer)) {
        toast("Cannot send file — peer not connected");
        return;
      }
    }
    toast(`Uploading ${file.name}…`);
    const form = new FormData();
    form.append("file", file);
    const up = await fetch("/api/upload", { method: "POST", body: form });
    if (!up.ok) { toast("Upload failed"); return; }
    const uploaded = await up.json();
    const res = await fetch("/api/transfer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recipient_hash: state.selectedPeer,
        path: uploaded.path,
        transport: peerTransport(state.selectedPeer),
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      toast(err.error || "File send failed");
    } else {
      const sent = await res.json();
      if (sent.id) updateTransferDock(sent);
      toast(`Sending ${file.name}…`);
      loadMessages();
    }
    renderTransfers();
  }

  /* ── Render ── */
  async function loadSerialSettings(selectedPort, selectedBaud) {
    const portEl = $("#set-serial-port");
    const baudEl = $("#set-serial-baud");
    if (!portEl || !baudEl) return;
    try {
      const [portsRes, baudRes] = await Promise.all([
        fetch("/api/serial/ports"),
        fetch("/api/serial/baud-rates"),
      ]);
      const portsData = await portsRes.json();
      const baudData = await baudRes.json();
      const ports = portsData.ports || [];
      portEl.innerHTML = ports.length
        ? ports.map((p) =>
            `<option value="${escapeHtml(p.device)}" ${p.device === selectedPort ? "selected" : ""}>${escapeHtml(p.description)}</option>`
          ).join("")
        : `<option value="">No serial devices detected</option>`;
      if (selectedPort && !ports.some((p) => p.device === selectedPort)) {
        portEl.innerHTML += `<option value="${escapeHtml(selectedPort)}" selected>${escapeHtml(selectedPort)} (saved)</option>`;
      }
      baudEl.innerHTML = (baudData.rates || [115200]).map((r) =>
        `<option value="${r}" ${r === selectedBaud ? "selected" : ""}>${r}</option>`
      ).join("");
    } catch (_) {
      portEl.innerHTML = `<option value="${escapeHtml(selectedPort)}">${escapeHtml(selectedPort || "—")}</option>`;
      baudEl.innerHTML = `<option value="57600">57600</option>`;
    }
  }

  async function loadInterfaces(selectEl, selectedIp) {
    const res = await fetch("/api/interfaces");
    const data = await res.json();
    state.interfaces = data.interfaces || [];
    selectEl.innerHTML = state.interfaces
      .map((i) => `<option value="${escapeHtml(i.ip)}" ${i.ip === selectedIp ? "selected" : ""}>${escapeHtml(i.label)}</option>`)
      .join("");
    if (!state.interfaces.length) {
      selectEl.innerHTML = '<option value="">127.0.0.1</option>';
    }
  }

  function fillSettingsForm(settings) {
    $("#set-name").value = settings.display_name || "";
    $("#set-web-port").value = settings.web_port || 9876;
    const preset = settings.message_retention_preset || "1w";
    if ($("#set-retention")) $("#set-retention").value = preset;
    $("#set-incoming").value = settings.incoming_files_dir || "";
    $("#set-shared").value = settings.shared_folder || "";
    $("#set-auto-announce").checked = !!settings.auto_announce;
    if ($("#set-enable-serial")) $("#set-enable-serial").checked = !!settings.enable_serial;
    loadSerialSettings(settings.serial_port || "", settings.serial_baud || 57600);
    if ($("#set-timezone")) {
      loadTimezones($("#set-timezone"), settings.timezone || "");
    }
    if ($("#set-show-clock")) {
      $("#set-show-clock").checked = settings.show_clock !== false;
    }
    if ($("#set-clock-source")) {
      $("#set-clock-source").value = settings.clock_source || "system";
    }
    if ($("#set-ntp-server")) {
      $("#set-ntp-server").value = settings.ntp_server || "pool.ntp.org";
    }
    toggleNtpField();
    applyClockVisibility();
    if (!$("#settings-window")?.classList.contains("hidden") || !state.interfacesLoaded) {
      loadInterfaces($("#set-lan-ip"), settings.lan_ip || "");
      state.interfacesLoaded = true;
    }
  }

  async function saveSettings(formData, complete) {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...formData, setup_complete: complete }),
    });
    if (!res.ok) {
      toast("Failed to save settings");
      return false;
    }
    state.settings = await res.json();
    toast(complete ? "Setup complete!" : "Settings saved");
    if (complete) $("#setup-overlay").classList.add("hidden");
    renderStatus(await (await fetch("/api/status")).json());
    return true;
  }

  function showSetupIfNeeded(settings) {
    if (!settings.setup_complete) {
      $("#setup-overlay").classList.remove("hidden");
      $("#setup-name").value = settings.display_name || "";
      $("#setup-web-port").value = settings.web_port || 9876;
      if ($("#setup-retention")) $("#setup-retention").value = settings.message_retention_preset || "1w";
      $("#setup-auto-announce").checked = !!settings.auto_announce;
      loadInterfaces($("#setup-lan-ip"), settings.lan_ip || "");
    }
  }

  async function loadTimezones(selectEl, selectedTz) {
    if (!selectEl) return;
    if (!state.timezones.length) {
      try {
        const res = await fetch("/api/timezones");
        const data = await res.json();
        state.timezones = data.timezones || [];
      } catch (_) {
        state.timezones = ["UTC"];
      }
    }
    const current = selectedTz || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    const options = state.timezones.includes(current)
      ? state.timezones
      : [current, ...state.timezones];
    selectEl.innerHTML = options.map((tz) =>
      `<option value="${escapeHtml(tz)}" ${tz === selectedTz ? "selected" : ""}>${escapeHtml(tz)}</option>`
    ).join("");
    if (!selectedTz) selectEl.value = current;
  }

  async function pollSystemStats() {
    try {
      const res = await fetch("/api/system");
      const data = await res.json();
      const cpuEl = $("#stat-cpu .stat-value");
      const tempEl = $("#stat-temp .stat-value");
      const headerClock = $("#header-clock");
      if (data.cpu_percent != null) {
        cpuEl.textContent = `${data.cpu_percent}%`;
        cpuEl.className = "stat-value" + (data.cpu_percent > 80 ? " hot" : data.cpu_percent > 50 ? " warn" : "");
      }
      if (data.cpu_temp_c != null) {
        tempEl.textContent = `${data.cpu_temp_c}°C`;
        tempEl.className = "stat-value" + (data.cpu_temp_c > 85 ? " hot" : data.cpu_temp_c > 70 ? " warn" : "");
      }
      if (state.settings.show_clock !== false && data.local_time) {
        if (headerClock) headerClock.textContent = data.local_time;
      }
    } catch (_) { /* ignore */ }
  }

  function peerEndpoint(peer) {
    if (peer.transport === "tcp" && peer.tcp_host) {
      return `${peer.tcp_host}:${peer.tcp_port || 7825}`;
    }
    if (peer.transport === "serial" && peer.address) {
      return peer.address;
    }
    return peer.hash_id.slice(0, 12) + "…";
  }

  async function renderNetworkGraph() {
    const canvas = $("#network-canvas");
    if (!canvas) return;
    const res = await fetch("/api/network");
    const data = await res.json();
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#0c0e14";
    ctx.fillRect(0, 0, w, h);

    const nodes = data.nodes || [];
    const edges = data.edges || [];
    const centerX = w / 2;
    const centerY = h / 2;
    const radius = Math.min(w, h) * 0.34;
    const positions = {};

    nodes.forEach((n, i) => {
      if (n.role === "self") {
        const selfNodes = nodes.filter((x) => x.role === "self");
        const idx = selfNodes.indexOf(n);
        const angle = (idx / Math.max(selfNodes.length, 1)) * Math.PI * 2 - Math.PI / 2;
        positions[n.id] = {
          x: centerX + Math.cos(angle) * 48,
          y: centerY + Math.sin(angle) * 48,
        };
      }
    });

    const others = nodes.filter((n) => n.role !== "self");
    others.forEach((n, i) => {
      const angle = (i / Math.max(others.length, 1)) * Math.PI * 2 - Math.PI / 2;
      positions[n.id] = {
        x: centerX + Math.cos(angle) * radius,
        y: centerY + Math.sin(angle) * radius,
      };
    });

    edges.forEach((e) => {
      const a = positions[e.from];
      const b = positions[e.to];
      if (!a || !b) return;
      const discovered = e.state === "discovered";
      ctx.strokeStyle = discovered
        ? "#8b95a8"
        : e.transport === "serial"
          ? "#3ecf8e"
          : "#5b8def";
      ctx.lineWidth = discovered ? 1.5 : 2;
      ctx.setLineDash(discovered ? [6, 5] : []);
      ctx.globalAlpha = discovered ? 0.5 : 1;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
    });

    nodes.forEach((n) => {
      const p = positions[n.id];
      if (!p) return;
      const color = n.role === "self" ? "#5b8def" : n.role === "trusted" ? "#3ecf8e" : "#f5a623";
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(p.x, p.y, n.role === "self" ? 18 : 14, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#e8ecf4";
      ctx.font = "11px DM Sans, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(n.label.slice(0, 14), p.x, p.y + 32);
      ctx.fillStyle = "#8b95a8";
      ctx.font = "9px JetBrains Mono, monospace";
      ctx.fillText(n.transport.toUpperCase(), p.x, p.y + 44);
    });
  }

  function renderStatus(data) {
    const ids = data.identities || {};
    state.myHashes = {};
    state.settings = data.settings || state.settings;
    let primary = null;

    if (data.version) $("#stat-version").textContent = `v${data.version}`;

    if (state.settings && Object.keys(state.settings).length) {
      if (!$("#settings-window")?.classList.contains("hidden") || state.settingsFormDirty) {
        fillSettingsForm(state.settings);
      }
      showSetupIfNeeded(state.settings);
      applyClockVisibility();
    }

    const idHtml = Object.entries(ids)
      .map(([t, id]) => {
        state.myHashes[t] = id.hash_id;
        if (t === "tcp" || !primary) primary = id;
        return `<div class="identity-card">
          <strong>${t}</strong>
          ${escapeHtml(id.name)}
          <code>${escapeHtml(id.hash_id)}</code>
          <div class="identity-actions">
            <button type="button" class="action-btn small" data-regen="${t}">Regenerate</button>
            <button type="button" class="action-btn small danger" data-del-id="${t}">Delete</button>
          </div>
        </div>`;
      })
      .join("");

    $("#identities").innerHTML = idHtml || '<div class="empty-hint">No identities</div>';

    const serialBtn = $("#btn-announce-serial");
    if (serialBtn) {
      serialBtn.disabled = !ids.serial;
      serialBtn.title = ids.serial ? "Announce on serial/RF" : "Enable serial in settings first";
    }

    if (primary) {
      state.myName = primary.name;
      $("#me-name").textContent = primary.name;
      $("#me-hash").textContent = primary.hash_id.slice(0, 16) + "…";
      setAvatar($("#me-avatar"), primary.name, primary.hash_id);
    }

    syncLinksFromStatus(data.links || []);

    loadPeers();
    if (state.selectedPeer) refreshPeerStatus(state.selectedPeer);
    renderTransfers();
  }

  function renderContacts() {
    const q = state.search.toLowerCase();
    const trustedIds = new Set(state.trusted.map((p) => p.hash_id));
    const list = state.peerTab === "trusted"
      ? state.trusted
      : state.peers.filter((p) => !trustedIds.has(p.hash_id));
    const filtered = list.filter(
      (p) =>
        !q ||
        p.name.toLowerCase().includes(q) ||
        p.hash_id.toLowerCase().includes(q)
    );

    const el = $("#contacts");
    const label = $("#contacts-label");
    if (label) label.textContent = state.peerTab === "trusted" ? "Trusted Peers" : "Discovered Peers";

    if (!filtered.length) {
      el.innerHTML = `<div class="contacts-empty">
        ${state.peerTab === "trusted"
          ? "No trusted peers.<br>Trust someone from <strong>Discovered</strong>."
          : "No peers yet.<br>Peers appear when others announce on the network."}
      </div>`;
      return;
    }

    el.innerHTML = filtered
      .map((p) => {
        const active = state.selectedPeer === p.hash_id ? " active" : "";
        const lm = state.linkMetrics[p.hash_id] || {};
        const metrics = [];
        const rtt = lm.rtt_ms ?? p.rtt_ms;
        if (rtt != null) metrics.push(`${Math.round(rtt)}ms`);
        const lq = lm.link_quality_pct ?? p.link_quality_pct;
        if (p.transport === "serial" && lq != null) metrics.push(`${lq}%`);
        const linked = isPeerLinked(p.hash_id);
        if (linked && !metrics.length) metrics.push("online");
        const endpoint = peerEndpoint(p);
        const meta = metrics.length
          ? `${p.transport.toUpperCase()} · ${endpoint} · ${metrics.join(" · ")}`
          : `${p.transport.toUpperCase()} · ${endpoint}`;
        const trustBtn = state.peerTab === "discovered" && !trustedIds.has(p.hash_id)
          ? `<button type="button" class="contact-trust" data-trust="${p.hash_id}">Trust</button>` : "";
        const menuBtn = state.peerTab === "trusted"
          ? `<button type="button" class="contact-menu-btn" data-menu="${p.hash_id}" data-name="${escapeHtml(p.name)}" title="Contact options" aria-label="Contact options">⋮</button>`
          : "";
        return `<button class="contact${active}" data-hash="${p.hash_id}" data-name="${escapeHtml(p.name)}">
          <div class="avatar" style="background:${hashColor(p.hash_id)}22;color:${hashColor(p.hash_id)};border-color:${hashColor(p.hash_id)}44">
            ${initials(p.name)}
          </div>
          <div class="contact-info">
            <div class="contact-name">${escapeHtml(p.name)}</div>
            <div class="contact-preview">${meta}</div>
          </div>
          <div class="contact-meta">${trustBtn}${menuBtn}${linked ? "●" : ""}</div>
        </button>`;
      })
      .join("");

    el.querySelectorAll(".contact").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        if (ev.target.closest(".contact-trust")) return;
        selectPeer(btn.dataset.hash, btn.dataset.name);
        closeSidebarMobile();
      });
    });
    el.querySelectorAll(".contact-trust").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        trustPeer(btn.dataset.trust);
      });
    });
    el.querySelectorAll(".contact-menu-btn").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        openContactMenu(btn.dataset.menu, btn.dataset.name, btn);
      });
    });
  }

  function selectPeer(hashId, name) {
    state.selectedPeer = hashId;
    state.selectedName = name;

    $("#chat-empty").classList.add("hidden");
    $("#chat-active").classList.remove("hidden");

    $("#chat-peer-name").textContent = name;
    setAvatar($("#peer-avatar"), name, hashId);

    refreshPeerStatus(hashId);

    $("#msg-input").disabled = false;
    $("#send-btn").disabled = false;
    $("#msg-input").focus();

    renderContacts();
    loadMessages();
    if (!isPeerLinked(hashId)) connectPeer(hashId, false);
  }

  function updatePeerStatus(text) {
    $("#chat-peer-meta").textContent = text;
  }

  function renderFileBubble(m, out) {
    const meta = m.metadata || {};
    const tid = meta.transfer_id || "";
    const live = state.transfers[tid] || meta;
    const size = live.size || meta.size || 0;
    const offset = live.offset || meta.offset || 0;
    const pct = size ? Math.min(100, Math.round((offset / size) * 100)) : 0;
    const speed = live.speed_mbps || meta.speed_mbps;
    const stateLabel = live.state || meta.state || "transferring";
    const filename = meta.filename || m.text || "file";
    const speedStr = speed ? ` · ${Number(speed).toFixed(2)} MB/s` : "";
    const fileUrl = tid ? `/api/transfers/${encodeURIComponent(tid)}/file` : "";

    if (m.msg_type === "image" && (stateLabel === "complete" || pct >= 100) && fileUrl) {
      return `<div class="file-bubble image-bubble">
        <a href="${fileUrl}" target="_blank" rel="noopener">
          <img src="${fileUrl}" alt="${escapeHtml(filename)}" class="chat-image" loading="lazy" />
        </a>
        <div class="file-name">${escapeHtml(filename)}</div>
        <div class="file-progress-meta">${formatBytes(size)} · complete</div>
      </div>`;
    }

    return `<div class="file-bubble" data-transfer="${escapeHtml(tid)}">
      <div class="file-icon">📎</div>
      <div class="file-info">
        <div class="file-name">${escapeHtml(filename)}</div>
        <div class="file-progress-meta">${formatBytes(offset)} / ${formatBytes(size)} · ${pct}%${speedStr} · ${stateLabel}</div>
        <div class="progress-track chat-progress"><div class="progress-fill" style="width:${pct}%"></div></div>
        ${stateLabel === "complete" && fileUrl ? `<a class="file-download" href="${fileUrl}" target="_blank" rel="noopener">Download</a>` : ""}
      </div>
    </div>`;
  }

  function renderMessages(msgs) {
    const el = $("#messages");
    state.messageCache = msgs;
    let lastDate = "";
    let html = "";

    msgs.forEach((m) => {
      const date = formatDate(m.timestamp);
      if (date !== lastDate) {
        html += `<div class="date-sep">${date}</div>`;
        lastDate = date;
      }
      const out = isOutgoing(m.sender_hash);
      const body = (m.msg_type === "file" || m.msg_type === "image")
        ? renderFileBubble(m, out)
        : escapeHtml(m.text);
      html += `<div class="bubble-row ${out ? "out" : "in"}" data-msg-id="${escapeHtml(m.id)}">
        <div class="bubble ${out ? "out" : "in"} ${m.msg_type === "image" ? "image" : ""}">
          ${body}
          <div class="bubble-meta">
            <span>${formatTime(m.timestamp)}</span>
            <span class="bubble-status ${m.status}"></span>
          </div>
        </div>
      </div>`;
    });

    el.innerHTML = html || '<div class="empty-hint" style="text-align:center;padding:2rem">No messages yet — say hello!</div>';
    el.scrollTop = el.scrollHeight;
  }

  function updateChatTransfer(data) {
    if (!data.id) return;
    state.transfers[data.id] = data;
    const peer = state.selectedPeer;
    if (!peer) return;
    const relevant = data.sender_hash === peer || data.recipient_hash === peer;
    if (!relevant) return;
    const idx = state.messageCache.findIndex((m) => m.metadata?.transfer_id === data.id);
    if (idx >= 0) {
      state.messageCache[idx].metadata = { ...state.messageCache[idx].metadata, ...data };
      renderMessages(state.messageCache);
    } else {
      loadMessages();
    }
  }

  function onNewMessage(m) {
    const forPeer = state.selectedPeer
      && (m.sender_hash === state.selectedPeer || m.recipient_hash === state.selectedPeer);

    if (!forPeer) {
      if (!isOutgoing(m.sender_hash)) {
        const sender = peerByHash(m.sender_hash);
        toast(`New message from ${sender?.name || m.sender_hash?.slice(0, 8) || "peer"}`);
      }
      return;
    }

    if (m.msg_type === "file" || m.msg_type === "image") {
      const idx = state.messageCache.findIndex((x) => x.id === m.id);
      if (idx >= 0) {
        state.messageCache[idx] = m;
        if (m.metadata?.transfer_id) state.transfers[m.metadata.transfer_id] = m.metadata;
        renderMessages(state.messageCache);
      } else {
        loadMessages();
      }
      return;
    }

    loadMessages();
  }

  async function renderTransfers() {
    const res = await fetch("/api/transfers");
    const transfers = await res.json();
    const el = $("#transfers");

    if (!transfers.length) {
      el.innerHTML = '<div class="empty-hint">No active transfers</div>';
      return;
    }

    el.innerHTML = transfers
      .map((t) => {
        const pct = t.size ? Math.round((t.offset / t.size) * 100) : 0;
        const speed = t.speed_mbps ? ` · ${t.speed_mbps.toFixed(2)} MB/s` : "";
        return `<div class="transfer-card">
          <div class="transfer-name">${escapeHtml(t.filename)}</div>
          <div class="transfer-state">${t.state} · ${pct}%${speed}</div>
          <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
        </div>`;
      })
      .join("");
  }

  /* ── UI helpers ── */
  function autoResize(el) {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  }

  function toggleNtpField() {
    const src = $("#set-clock-source")?.value || "system";
    $("#ntp-server-field")?.classList.toggle("hidden", src !== "ntp");
  }

  function switchSettingsTab(tab) {
    document.querySelectorAll(".settings-tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === tab);
    });
    document.querySelectorAll(".settings-panel").forEach((p) => {
      p.classList.toggle("active", p.dataset.panel === tab);
    });
  }

  function openSettings() {
    $("#settings-window").classList.remove("hidden");
    $("#settings-window").setAttribute("aria-hidden", "false");
    state.settingsFormDirty = true;
    const s = state.settings || {};
    fillSettingsForm(s);
    loadSerialSettings(s.serial_port || "", s.serial_baud || 57600);
  }

  function closeSettings() {
    $("#settings-window").classList.add("hidden");
    $("#settings-window").setAttribute("aria-hidden", "true");
    state.settingsFormDirty = false;
  }

  function updateTransferDock(data) {
    const dock = $("#transfer-dock");
    if (!dock || !data) return;
    const active = ["transferring", "accepted", "offered"].includes(data.state);
    if (!active) {
      hideTransferDockIfDone(data.id);
      return;
    }
    dock.classList.remove("hidden");
    const pct = data.size ? Math.min(100, Math.round((data.offset / data.size) * 100)) : 0;
    const speed = data.speed_mbps ? `${Number(data.speed_mbps).toFixed(2)} MB/s` : "";
    $("#transfer-dock-title").textContent = data.filename || "Transfer";
    $("#transfer-dock-meta").textContent = `${pct}% · ${formatBytes(data.offset)} / ${formatBytes(data.size)}${speed ? ` · ${speed}` : ""}`;
    $("#transfer-dock-fill").style.width = `${pct}%`;
    dock.dataset.transferId = data.id;
  }

  function hideTransferDockIfDone(transferId) {
    const dock = $("#transfer-dock");
    if (!dock || dock.classList.contains("hidden")) return;
    if (!transferId || dock.dataset.transferId === transferId) {
      dock.classList.add("hidden");
      delete dock.dataset.transferId;
    }
  }

  async function cancelActiveTransfer() {
    const dock = $("#transfer-dock");
    const id = dock?.dataset.transferId;
    if (!id) return;
    await fetch(`/api/transfers/${encodeURIComponent(id)}/cancel`, { method: "POST" });
    toast("Transfer cancelled");
    hideTransferDockIfDone(id);
    renderTransfers();
    loadMessages();
  }

  function closeSidebarMobile() {
    $("#sidebar").classList.remove("open");
  }

  /* ── Events ── */
  $("#btn-announce-tcp")?.addEventListener("click", () => announceTransport("tcp"));
  $("#btn-announce-serial")?.addEventListener("click", () => announceTransport("serial"));

  $("#btn-settings").addEventListener("click", openSettings);
  $("#btn-close-settings").addEventListener("click", closeSettings);
  $("#settings-window-overlay").addEventListener("click", closeSettings);
  document.querySelectorAll(".settings-tab").forEach((tab) => {
    tab.addEventListener("click", () => switchSettingsTab(tab.dataset.tab));
  });
  $("#set-clock-source")?.addEventListener("change", toggleNtpField);
  $("#transfer-dock-cancel")?.addEventListener("click", cancelActiveTransfer);

  $("#btn-back").addEventListener("click", () => {
    $("#chat-active").classList.add("hidden");
    $("#chat-empty").classList.remove("hidden");
    $("#sidebar").classList.add("open");
    state.selectedPeer = null;
    renderContacts();
  });

  $("#peer-search").addEventListener("input", (e) => {
    state.search = e.target.value;
    renderContacts();
  });

  $("#send-btn").addEventListener("click", sendMessage);

  $("#msg-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  $("#msg-input").addEventListener("input", (e) => autoResize(e.target));

  $("#btn-file").addEventListener("click", () => $("#file-input").click());

  $("#file-input").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (file) sendFile(file);
    e.target.value = "";
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSettings();
  });

  function settingsPayload(prefix) {
    return {
      display_name: $(`#${prefix}-name`).value.trim(),
      web_port: parseInt($(`#${prefix}-web-port`).value, 10),
      message_retention_preset: $(`#${prefix}-retention`)?.value || "1w",
      incoming_files_dir: $(`#${prefix}-incoming`)?.value.trim() || "",
      shared_folder: $(`#${prefix}-shared`)?.value.trim() || "",
      lan_ip: $(`#${prefix}-lan-ip`)?.value || "",
      auto_announce: $(`#${prefix}-auto-announce`)?.checked || false,
      enable_serial: $("#set-enable-serial")?.checked || false,
      serial_port: $("#set-serial-port")?.value || "",
      serial_baud: parseInt($("#set-serial-baud")?.value || "57600", 10),
      timezone: $("#set-timezone")?.value || "",
      show_clock: $("#set-show-clock")?.checked !== false,
      clock_source: $("#set-clock-source")?.value || "system",
      ntp_server: $("#set-ntp-server")?.value.trim() || "pool.ntp.org",
    };
  }

  async function browseFolder(path) {
    const url = path ? `/api/browse?path=${encodeURIComponent(path)}` : "/api/browse";
    const res = await fetch(url);
    const data = await res.json();
    $("#folder-crumb").textContent = data.path;
    let html = "";
    if (data.parent && data.parent !== data.path) {
      html += `<button type="button" class="folder-entry" data-path="${escapeHtml(data.parent)}">..</button>`;
    }
    html += (data.entries || []).filter((e) => e.type === "dir").map((e) =>
      `<button type="button" class="folder-entry" data-path="${escapeHtml(e.path)}">${escapeHtml(e.name)}</button>`
    ).join("");
    $("#folder-list").innerHTML = html || '<div class="empty-hint">No folders</div>';
    $("#folder-list").querySelectorAll(".folder-entry").forEach((btn) => {
      btn.addEventListener("click", () => browseFolder(btn.dataset.path));
    });
  }

  $("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await saveSettings(settingsPayload("set"), false);
  });

  $("#setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const port = parseInt($("#setup-web-port").value, 10);
    await saveSettings(settingsPayload("setup"), true);
    if (port !== location.port) {
      toast(`Restart with --port ${port} to apply new HTTPS port`);
    }
  });

  document.querySelectorAll(".peer-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.peerTab = tab.dataset.tab;
      document.querySelectorAll(".peer-tab").forEach((t) => t.classList.toggle("active", t === tab));
      renderContacts();
    });
  });

  $("#stat-version")?.addEventListener("click", async () => {
    const res = await fetch("/api/release-notes");
    const data = await res.json();
    $("#release-notes-body").textContent = data.notes;
    $("#release-modal").classList.add("open");
  });

  $("#btn-restart")?.addEventListener("click", async () => {
    if (!confirm("Restart SRLTCP?")) return;
    await fetch("/api/restart", { method: "POST" });
    toast("Restarting…");
    setTimeout(() => location.reload(), 3000);
  });

  document.addEventListener("click", (e) => {
    const browseBtn = e.target.closest("[data-browse]");
    if (browseBtn) {
      e.preventDefault();
      state.folderTarget = browseBtn.dataset.browse;
      browseFolder(null);
      $("#folder-modal").classList.add("open");
      return;
    }
    const menuBtn = e.target.closest(".contact-menu-btn");
    if (menuBtn) return;
    if (!e.target.closest("#contact-menu")) closeContactMenu();
  });

  $("#contact-menu")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn || !state.contactMenuTarget) return;
    const { hashId, name } = state.contactMenuTarget;
    const peer = state.trusted.find((p) => p.hash_id === hashId);
    switch (btn.dataset.action) {
      case "clear-chat":
        await clearChatHistory(hashId);
        break;
      case "rename":
        await renameContact(hashId, name);
        break;
      case "block":
        if (peer?.blocked) {
          closeContactMenu();
          await fetch(`/api/trusted/${encodeURIComponent(hashId)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ blocked: false }),
          });
          toast(`${name} unblocked`);
          loadPeers();
        } else {
          await blockContact(hashId, name);
        }
        break;
      case "delete":
        await deleteTrusted(hashId, name);
        break;
      default:
        break;
    }
  });

  $("#folder-select")?.addEventListener("click", () => {
    if (state.folderTarget) $(`#${state.folderTarget}`).value = $("#folder-crumb").textContent;
    $("#folder-modal").classList.remove("open");
  });

  $("#folder-cancel")?.addEventListener("click", () => $("#folder-modal").classList.remove("open"));
  $("#release-close")?.addEventListener("click", () => $("#release-modal").classList.remove("open"));

  $("#btn-network-viz")?.addEventListener("click", async () => {
    $("#network-modal").classList.add("open");
    await renderNetworkGraph();
  });
  $("#network-close")?.addEventListener("click", () => $("#network-modal").classList.remove("open"));
  $("#network-refresh")?.addEventListener("click", () => renderNetworkGraph());

  $("#identities")?.addEventListener("click", async (e) => {
    const regen = e.target.closest("[data-regen]");
    const del = e.target.closest("[data-del-id]");
    if (regen && confirm(`Regenerate ${regen.dataset.regen} identity?`)) {
      await fetch(`/api/identities/${regen.dataset.regen}/regenerate`, { method: "POST" });
      renderStatus(await (await fetch("/api/status")).json());
    }
    if (del && confirm(`Delete ${del.dataset.delId} identity?`)) {
      await fetch(`/api/identities/${del.dataset.delId}`, { method: "DELETE" });
      renderStatus(await (await fetch("/api/status")).json());
    }
  });

  /* ── Init ── */
  connectWs();
  pollSystemStats();
  setInterval(pollSystemStats, 10000);
  setInterval(async () => {
    if (state.settings.show_clock === false) return;
    try {
      const res = await fetch("/api/system");
      const data = await res.json();
      if (data.local_time && $("#header-clock")) {
        $("#header-clock").textContent = data.local_time;
      }
    } catch (_) { /* ignore */ }
  }, 1000);
  setInterval(loadPeers, 30000);

  fetch("/api/settings")
    .then((r) => r.json())
    .then((s) => {
      state.settings = s;
      showSetupIfNeeded(s);
      fillSettingsForm(s);
      applyClockVisibility();
    })
    .catch(() => {});

  fetch("/api/status")
    .then((r) => r.json())
    .then(renderStatus)
    .catch(() => toast("Failed to load status"));
})();