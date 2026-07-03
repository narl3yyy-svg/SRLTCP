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
    links: {},
    ws: null,
    search: "",
    settings: {},
    interfaces: [],
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
          toast(`Discovered: ${data.name}`);
          break;
        case "link_up":
          state.links[data.hash_id] = true;
          loadPeers();
          if (state.selectedPeer === data.hash_id) updatePeerStatus("Encrypted · Online");
          toast(`Connected to ${data.name}`);
          logActivity(`Link up: ${data.name}`);
          break;
        case "transfer_progress":
        case "transfer_complete":
          renderTransfers();
          if (type === "transfer_complete") toast(`Transfer complete: ${data.filename}`);
          break;
        case "transport_event":
          logActivity(`Transport: ${data.kind}`);
          break;
      }
    };
  }

  /* ── API ── */
  async function loadPeers() {
    const res = await fetch("/api/peers");
    state.peers = await res.json();
    renderContacts();
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

  async function announce() {
    await fetch("/api/announce", { method: "POST" });
    toast("Announced on all transports");
    logActivity("Announced presence");
    setTimeout(loadPeers, 800);
  }

  async function connectPeer(hashId) {
    await fetch("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hash_id: hashId, transport: "tcp" }),
    });
    updatePeerStatus("Handshaking…");
    logActivity(`Connecting to ${hashId.slice(0, 12)}…`);
  }

  async function sendMessage() {
    const input = $("#msg-input");
    const text = input.value.trim();
    if (!text || !state.selectedPeer) return;

    input.value = "";
    autoResize(input);

    await fetch("/api/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recipient_hash: state.selectedPeer,
        text,
        transport: "tcp",
      }),
    });
  }

  async function sendFile(file) {
    if (!state.selectedPeer || !file) return;
    toast(`Sending ${file.name}…`);
    const res = await fetch("/api/transfer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recipient_hash: state.selectedPeer,
        path: file.path || file.name,
        transport: "tcp",
      }),
    });
    if (!res.ok) toast("File send failed — use API with local path");
    renderTransfers();
  }

  /* ── Render ── */
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
    $("#set-retention").value = settings.message_retention_hours || 168;
    $("#set-incoming").value = settings.incoming_files_dir || "";
    $("#set-shared").value = settings.shared_folder || "";
    $("#set-auto-announce").checked = settings.auto_announce !== false;
    loadInterfaces($("#set-lan-ip"), settings.lan_ip || "");
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
      $("#setup-retention").value = settings.message_retention_hours || 168;
      $("#setup-auto-announce").checked = settings.auto_announce !== false;
      loadInterfaces($("#setup-lan-ip"), settings.lan_ip || "");
    }
  }

  async function pollSystemStats() {
    try {
      const res = await fetch("/api/system");
      const data = await res.json();
      const cpuEl = $("#stat-cpu .stat-value");
      const tempEl = $("#stat-temp .stat-value");
      if (data.cpu_percent != null) {
        cpuEl.textContent = `${data.cpu_percent}%`;
        cpuEl.className = "stat-value" + (data.cpu_percent > 80 ? " hot" : data.cpu_percent > 50 ? " warn" : "");
      }
      if (data.cpu_temp_c != null) {
        tempEl.textContent = `${data.cpu_temp_c}°C`;
        tempEl.className = "stat-value" + (data.cpu_temp_c > 85 ? " hot" : data.cpu_temp_c > 70 ? " warn" : "");
      }
    } catch (_) { /* ignore */ }
  }

  function renderStatus(data) {
    const ids = data.identities || {};
    state.myHashes = {};
    state.settings = data.settings || state.settings;
    let primary = null;

    if (data.version) $("#stat-version").textContent = `v${data.version}`;

    if (state.settings && Object.keys(state.settings).length) {
      fillSettingsForm(state.settings);
      showSetupIfNeeded(state.settings);
    }

    const idHtml = Object.entries(ids)
      .map(([t, id]) => {
        state.myHashes[t] = id.hash_id;
        if (t === "tcp" || !primary) primary = id;
        return `<div class="identity-card">
          <strong>${t}</strong>
          ${escapeHtml(id.name)}
          <code>${escapeHtml(id.hash_id)}</code>
        </div>`;
      })
      .join("");

    $("#identities").innerHTML = idHtml || '<div class="empty-hint">No identities</div>';

    if (primary) {
      state.myName = primary.name;
      $("#me-name").textContent = primary.name;
      $("#me-hash").textContent = primary.hash_id.slice(0, 16) + "…";
      setAvatar($("#me-avatar"), primary.name, primary.hash_id);
    }

    (data.links || []).forEach((l) => {
      state.links[l.hash_id] = l.handshake_complete;
    });

    loadPeers();
    renderTransfers();
  }

  function renderContacts() {
    const q = state.search.toLowerCase();
    const filtered = state.peers.filter(
      (p) =>
        !q ||
        p.name.toLowerCase().includes(q) ||
        p.hash_id.toLowerCase().includes(q)
    );

    const el = $("#contacts");
    if (!filtered.length) {
      el.innerHTML = `<div class="contacts-empty">
        No peers found.<br>Click <strong>Announce</strong> to discover nearby devices.
      </div>`;
      return;
    }

    el.innerHTML = filtered
      .map((p) => {
        const linked = state.links[p.hash_id];
        const active = state.selectedPeer === p.hash_id ? " active" : "";
        return `<button class="contact${active}" data-hash="${p.hash_id}" data-name="${escapeHtml(p.name)}">
          <div class="avatar" style="background:${hashColor(p.hash_id)}22;color:${hashColor(p.hash_id)};border-color:${hashColor(p.hash_id)}44">
            ${initials(p.name)}
          </div>
          <div class="contact-info">
            <div class="contact-name">${escapeHtml(p.name)}</div>
            <div class="contact-preview">${p.transport.toUpperCase()} · ${p.hash_id.slice(0, 10)}…</div>
          </div>
          <div class="contact-meta">${linked ? "●" : ""}</div>
        </button>`;
      })
      .join("");

    el.querySelectorAll(".contact").forEach((btn) => {
      btn.addEventListener("click", () => {
        selectPeer(btn.dataset.hash, btn.dataset.name);
        closeSidebarMobile();
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

    const linked = state.links[hashId];
    updatePeerStatus(linked ? "Encrypted · Online" : "Connecting…");
    $(".status-dot").className = `status-dot ${linked ? "online" : "pending"}`;

    $("#msg-input").disabled = false;
    $("#send-btn").disabled = false;
    $("#msg-input").focus();

    renderContacts();
    loadMessages();
    connectPeer(hashId);
  }

  function updatePeerStatus(text) {
    $("#chat-peer-meta").textContent = text;
  }

  function renderMessages(msgs) {
    const el = $("#messages");
    let lastDate = "";
    let html = "";

    msgs.forEach((m) => {
      const date = formatDate(m.timestamp);
      if (date !== lastDate) {
        html += `<div class="date-sep">${date}</div>`;
        lastDate = date;
      }
      const out = isOutgoing(m.sender_hash);
      html += `<div class="bubble-row ${out ? "out" : "in"}">
        <div class="bubble ${out ? "out" : "in"}">
          ${escapeHtml(m.text)}
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

  function onNewMessage(m) {
    if (
      state.selectedPeer &&
      m.sender_hash !== state.selectedPeer &&
      m.recipient_hash !== state.selectedPeer
    ) {
      if (!isOutgoing(m.sender_hash)) toast(`New message from peer`);
      return;
    }

    if (state.selectedPeer) {
      loadMessages();
    }
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
        return `<div class="transfer-card">
          <div class="transfer-name">${escapeHtml(t.filename)}</div>
          <div class="transfer-state">${t.state} · ${pct}%</div>
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

  function openDrawer() {
    $("#drawer").classList.add("open");
    $("#drawer").setAttribute("aria-hidden", "false");
  }

  function closeDrawer() {
    $("#drawer").classList.remove("open");
    $("#drawer").setAttribute("aria-hidden", "true");
  }

  function closeSidebarMobile() {
    $("#sidebar").classList.remove("open");
  }

  /* ── Events ── */
  $("#btn-announce").addEventListener("click", announce);

  $("#btn-settings").addEventListener("click", openDrawer);
  $("#btn-close-drawer").addEventListener("click", closeDrawer);
  $("#drawer-overlay").addEventListener("click", closeDrawer);

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

  $("#btn-file").addEventListener("click", () => {
    toast("Enter file path via API or drag-drop in a future update");
    $("#file-input").click();
  });

  $("#file-input").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (file) toast(`To send files, use: curl -X POST /api/transfer with path: ${file.name}`);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
  });

  $("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await saveSettings({
      display_name: $("#set-name").value.trim(),
      web_port: parseInt($("#set-web-port").value, 10),
      message_retention_hours: parseInt($("#set-retention").value, 10),
      incoming_files_dir: $("#set-incoming").value.trim(),
      shared_folder: $("#set-shared").value.trim(),
      lan_ip: $("#set-lan-ip").value,
      auto_announce: $("#set-auto-announce").checked,
    }, true);
  });

  $("#setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const port = parseInt($("#setup-web-port").value, 10);
    await saveSettings({
      display_name: $("#setup-name").value.trim(),
      web_port: port,
      message_retention_hours: parseInt($("#setup-retention").value, 10),
      incoming_files_dir: $("#setup-incoming").value.trim(),
      shared_folder: $("#setup-shared").value.trim(),
      lan_ip: $("#setup-lan-ip").value,
      auto_announce: $("#setup-auto-announce").checked,
    }, true);
    if (port !== location.port) {
      toast(`Restart with --port ${port} to apply new HTTPS port`);
    }
  });

  /* ── Init ── */
  connectWs();
  pollSystemStats();
  setInterval(pollSystemStats, 2000);
  setInterval(loadPeers, 15000);

  fetch("/api/settings")
    .then((r) => r.json())
    .then((s) => { state.settings = s; showSetupIfNeeded(s); fillSettingsForm(s); })
    .catch(() => {});

  fetch("/api/status")
    .then((r) => r.json())
    .then(renderStatus)
    .catch(() => toast("Failed to load status"));
})();