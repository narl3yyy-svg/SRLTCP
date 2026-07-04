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
    unread: {},
    transferCooldownUntil: {},
    mediaZoom: 1,
    mediaPan: { x: 0, y: 0 },
    mediaDragging: false,
    mediaDragStart: null,
    transferPatchTimer: null,
    pendingTransferPatches: new Map(),
    dropTargetHash: null,
    folderSendTarget: null,
    finishedTransferIds: new Set(),
    wanModalTarget: null,
    shareMode: "browse",
    shareGrants: { local: [], remote: [] },
    shareListing: { ownerHash: null, grantId: null, entries: [] },
  };

  const UNREAD_STORAGE_KEY = "srltcp-unread-v1";
  const TRANSFER_COOLDOWN_MS = 45000;

  const COLORS = [
    "#5b8def", "#3ecf8e", "#f5a623", "#f07178",
    "#c678dd", "#56b6c2", "#e5c07b", "#61afef",
  ];

  const ICON_COPY = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
  const ICON_TRASH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>`;
  const ACTIVE_TRANSFER_STATES = new Set(["transferring", "accepted", "offered"]);

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

  function loadUnreadState() {
    try {
      const raw = localStorage.getItem(UNREAD_STORAGE_KEY);
      if (raw) state.unread = JSON.parse(raw) || {};
    } catch (_) {
      state.unread = {};
    }
  }

  function saveUnreadState() {
    try {
      localStorage.setItem(UNREAD_STORAGE_KEY, JSON.stringify(state.unread));
    } catch (_) { /* ignore */ }
  }

  function unreadCount(hashId) {
    return state.unread[hashId] || 0;
  }

  function bumpUnread(hashId) {
    if (!hashId) return;
    state.unread[hashId] = (state.unread[hashId] || 0) + 1;
    saveUnreadState();
    renderContacts();
  }

  function clearUnread(hashId) {
    if (!hashId || !state.unread[hashId]) return;
    delete state.unread[hashId];
    saveUnreadState();
    renderContacts();
  }

  function inTransferCooldown(hashId) {
    const until = state.transferCooldownUntil[hashId];
    return until && Date.now() < until;
  }

  function markTransferCooldown(...hashIds) {
    const until = Date.now() + TRANSFER_COOLDOWN_MS;
    hashIds.filter(Boolean).forEach((id) => {
      state.transferCooldownUntil[id] = until;
    });
  }

  function toast(msg, type = "info") {
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), type === "error" ? 5200 : 3600);
  }

  function notifyUser(title, body, { tag, silent = false } = {}) {
    if (!silent) toast(body, "info");
    if (!document.hidden || silent) return;
    if (!("Notification" in window)) return;
    const show = () => {
      try {
        new Notification(title, { body, tag, icon: "/static/app.css" });
      } catch (_) { /* ignore */ }
    };
    if (Notification.permission === "granted") {
      show();
    } else if (Notification.permission !== "denied") {
      Notification.requestPermission().then((p) => {
        if (p === "granted") show();
      });
    }
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

  function transportBadge(transport) {
    const t = (transport || "tcp").toLowerCase();
    const label = t === "serial" ? "SERIAL" : "TCP";
    return `<span class="transport-badge ${t}">${label}</span>`;
  }

  let networkAnimFrame = null;
  let networkGraphData = null;

  function isPeerLinked(hashId) {
    if (state.links[hashId]) return true;
    const link = (state._lastLinks || []).find((l) => l.hash_id === hashId);
    return !!(link && link.handshake_complete);
  }

  function activeLinkForPeer(hashId) {
    return (state._lastLinks || []).find(
      (l) => l.hash_id === hashId && l.handshake_complete
    );
  }

  function activeLinkTransport(hashId) {
    const link = activeLinkForPeer(hashId);
    if (link?.transport) return link.transport;
    return peerTransport(hashId);
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
          if (data.transport) {
            state._lastLinks = state._lastLinks || [];
            const existing = state._lastLinks.find((l) => l.hash_id === data.hash_id);
            if (existing) {
              existing.handshake_complete = true;
              existing.transport = data.transport;
            } else {
              state._lastLinks.push({
                hash_id: data.hash_id,
                handshake_complete: true,
                transport: data.transport,
              });
            }
          }
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
          if (inTransferCooldown(data.hash_id)) {
            if (state.selectedPeer === data.hash_id) refreshPeerStatus(data.hash_id);
            break;
          }
          delete state.links[data.hash_id];
          delete state.linkMetrics[data.hash_id];
          loadPeers();
          if (state.selectedPeer === data.hash_id) {
            refreshPeerStatus(data.hash_id);
          }
          notifyUser(
            "Disconnected",
            data.name || data.hash_id?.slice(0, 8) || "Peer",
            { tag: `down-${data.hash_id}` }
          );
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
          syncTransferMessage(data);
          if (state.selectedPeer) updateChatTransfer(data);
          if (type === "transfer_complete") {
            markTransferCooldown(data.sender_hash, data.recipient_hash);
            notifyUser(
              "Transfer complete",
              data.filename || "File received",
              { tag: `transfer-${data.id}` }
            );
            hideTransferDockIfDone(data.id);
            refreshTransferBubble(data.id, data, { scroll: !!state.selectedPeer });
          }
          break;
        case "share_offer":
          loadShareGrants().then(() => {
            if (state.selectedPeer === data.hash_id) renderShareGrants();
            toast(`Shared folder offered: ${data.label || "folder"}`);
          });
          break;
        case "share_listing":
          if (data.grant_id) {
            state.shareListing = {
              ownerHash: data.hash_id,
              grantId: data.grant_id,
              entries: data.entries || [],
            };
            renderShareEntries();
          }
          break;
        case "share_revoked":
          loadShareGrants().then(() => {
            renderShareGrants();
            toast("Shared folder access revoked");
          });
          break;
        case "transport_event":
          logActivity(`Transport: ${data.kind}${data.hash_id ? ` (${data.hash_id.slice(0, 8)})` : ""}`);
          if (data.kind === "disconnected" && data.hash_id) {
            if (inTransferCooldown(data.hash_id)) {
              if (state.selectedPeer === data.hash_id) {
                updatePeerStatus("Encrypted · Online");
                $(".status-dot").className = "status-dot online";
              }
              break;
            }
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
        tcp_host: discovered?.tcp_host || "",
        tcp_port: discovered?.tcp_port || 7825,
        public_key: discovered?.public_key || "",
      }),
    });
    toast("Peer trusted");
    state.peers = state.peers.filter((p) => p.hash_id !== hashId);
    loadPeers();
  }

  function openAddContactModal() {
    $("#add-contact-modal")?.classList.add("open");
    $("#add-contact-hash")?.focus();
  }

  function closeAddContactModal() {
    $("#add-contact-modal")?.classList.remove("open");
  }

  async function saveManualContact() {
    const hashId = ($("#add-contact-hash")?.value || "").trim().toLowerCase();
    const name = ($("#add-contact-name")?.value || "").trim() || "Peer";
    const transport = $("#add-contact-transport")?.value || "tcp";
    const tcpHost = ($("#add-contact-host")?.value || "").trim();
    const tcpPort = parseInt($("#add-contact-port")?.value || "7825", 10);
    if (!/^[0-9a-f]{32}$/.test(hashId)) {
      toast("Hash ID must be exactly 32 hex characters");
      return;
    }
    const res = await fetch("/api/trusted", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        hash_id: hashId,
        name,
        transport,
        tcp_host: tcpHost,
        tcp_port: tcpPort,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      toast(data.error || "Failed to add contact");
      return;
    }
    closeAddContactModal();
    state.peerTab = "trusted";
    document.querySelectorAll(".peer-tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === "trusted");
    });
    toast(`Added ${name}`);
    await loadPeers();
    selectPeer(hashId, name);
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

  function positionContactMenu(menu, anchor) {
    const margin = 8;
    menu.classList.remove("hidden");
    menu.setAttribute("aria-hidden", "false");
    const rect = anchor.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    const viewH = window.innerHeight;
    const viewW = window.innerWidth;
    let top = rect.bottom + 4;
    let left = Math.max(margin, rect.right - menuRect.width);
    if (left + menuRect.width > viewW - margin) {
      left = Math.max(margin, viewW - menuRect.width - margin);
    }
    if (top + menuRect.height > viewH - margin) {
      const above = rect.top - menuRect.height - 4;
      top = above >= margin ? above : Math.max(margin, viewH - menuRect.height - margin);
    }
    menu.style.top = `${top}px`;
    menu.style.left = `${left}px`;
  }

  function openContactMenu(hashId, name, anchor) {
    const menu = $("#contact-menu");
    if (!menu) return;
    state.contactMenuTarget = { hashId, name };
    const peer = state.trusted.find((p) => p.hash_id === hashId);
    const blockBtn = menu.querySelector('[data-action="block"]');
    if (blockBtn) {
      blockBtn.textContent = peer?.blocked ? "Unblock contact" : "Block contact";
    }
    positionContactMenu(menu, anchor);
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
    const btn = transport === "tcp" ? $("#btn-announce-tcp") : $("#btn-announce-serial");
    const status = state.transportStatus?.[transport];
    if (btn?.disabled || status && !status.active) {
      const hint = transport === "serial"
        ? "Serial port not open — enable serial in settings and check /dev permissions"
        : "TCP transport unavailable — restart the node";
      toast(hint);
      return;
    }
    const res = await fetch(`/api/announce?transport=${transport}`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || `Announce ${transport.toUpperCase()} failed`);
      return;
    }
    const bursts = data.bursts || 3;
    toast(`Announced on ${transport.toUpperCase()} (${bursts}× burst)`);
    logActivity(`Announced on ${transport}`);
    setTimeout(loadPeers, 800);
    setTimeout(loadPeers, 2000);
    if (transport === "serial") setTimeout(loadPeers, 4000);
  }

  function formatLatency(hashId) {
    const m = state.linkMetrics[hashId] || {};
    const link = (state._lastLinks || []).find((l) => l.hash_id === hashId);
    const rtt = m.rtt_ms ?? link?.rtt_ms;
    if (rtt != null) return `${Math.round(rtt)} ms`;
    return null;
  }

  function updateChatTransportBadge(hashId, linked) {
    const badge = $("#chat-peer-transport");
    if (!badge) return;
    if (!linked || !hashId) {
      badge.classList.add("hidden");
      badge.setAttribute("aria-hidden", "true");
      return;
    }
    const transport = activeLinkTransport(hashId);
    const label = transport === "serial" ? "SERIAL" : "TCP";
    badge.textContent = label;
    badge.className = `transport-badge header-badge ${transport}`;
    badge.removeAttribute("aria-hidden");
  }

  function refreshPeerStatus(hashId) {
    const linked = isPeerLinked(hashId) || inTransferCooldown(hashId);
    const latency = formatLatency(hashId);
    updateChatTransportBadge(hashId, linked);
    if ((linked || inTransferCooldown(hashId)) && latency) {
      updatePeerStatus(`Encrypted · Online · ${latency}`);
      $(".status-dot").className = "status-dot online";
    } else if (linked || inTransferCooldown(hashId)) {
      updatePeerStatus("Encrypted · Online");
      $(".status-dot").className = "status-dot online";
    } else {
      updatePeerStatus("Handshaking…");
      $(".status-dot").className = "status-dot pending";
    }
    $("#msg-input").disabled = !hashId;
    $("#send-btn").disabled = !hashId;
  }

  async function waitForHandshake(hashId, maxMs = 12000) {
    const deadline = Date.now() + maxMs;
    while (Date.now() < deadline) {
      if (isPeerLinked(hashId)) return true;
      try {
        const st = await (await fetch("/api/status")).json();
        syncLinksFromStatus(st.links || []);
        if (isPeerLinked(hashId)) return true;
      } catch (_) { /* retry */ }
      await new Promise((r) => setTimeout(r, 400));
    }
    return isPeerLinked(hashId);
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
        const ready = await waitForHandshake(hashId);
        if (ready) {
          state.links[hashId] = true;
          data.handshake_complete = true;
          if (state.selectedPeer === hashId) refreshPeerStatus(hashId);
        } else if (state.selectedPeer === hashId) {
          updatePeerStatus("Handshake timed out");
          toast("Handshake timed out — try again");
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

  async function sendFileToPeer(file, hashId, peerName) {
    if (!hashId || !file) return false;
    if (!isPeerLinked(hashId)) {
      toast(`Connecting to ${peerName || "peer"}…`);
      await connectPeer(hashId, false);
      if (!isPeerLinked(hashId)) {
        toast("Cannot send file — peer not connected");
        return false;
      }
    }
    toast(`Uploading ${file.name}…`);
    const form = new FormData();
    form.append("file", file);
    const up = await fetch("/api/upload", { method: "POST", body: form });
    if (!up.ok) {
      toast("Upload failed");
      return false;
    }
    const uploaded = await up.json();
    const res = await fetch("/api/transfer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recipient_hash: hashId,
        path: uploaded.path,
        transport: peerTransport(hashId),
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      toast(err.error || "File send failed");
      return false;
    }
    const sent = await res.json();
    if (sent.id) {
      state.finishedTransferIds.delete(sent.id);
      updateTransferDock(sent);
    }
    toast(`Sending ${file.name}…`);
    if (state.selectedPeer === hashId) loadMessages();
    renderTransfers();
    return true;
  }

  async function sendFile(file) {
    if (!state.selectedPeer || !file) return;
    await sendFileToPeer(file, state.selectedPeer, state.selectedName);
  }

  async function sendFolderToPeer(folderPath, hashId, peerName) {
    if (!hashId || !folderPath) return false;
    if (!isPeerLinked(hashId)) {
      toast(`Connecting to ${peerName || "peer"}…`);
      await connectPeer(hashId, false);
      if (!isPeerLinked(hashId)) {
        toast("Cannot send folder — peer not connected");
        return false;
      }
    }
    const folderName = folderPath.split("/").filter(Boolean).pop() || "folder";
    toast(`Zipping and sending ${folderName}…`);
    const res = await fetch("/api/transfer-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recipient_hash: hashId,
        path: folderPath,
        transport: peerTransport(hashId),
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      toast(err.error || "Folder send failed");
      return false;
    }
    const sent = await res.json();
    if (sent.id) {
      state.finishedTransferIds.delete(sent.id);
      updateTransferDock(sent);
    }
    toast(`Sending folder ${folderName}.zip…`);
    if (state.selectedPeer === hashId) loadMessages();
    renderTransfers();
    return true;
  }

  function openFolderSendPicker(hashId, peerName) {
    state.folderSendTarget = { hashId, peerName };
    state.folderTarget = "folder-send";
    browseFolder(null);
    $("#folder-modal")?.classList.add("open");
  }

  async function sendDroppedFiles(files, hashId, peerName) {
    const list = [...files].filter((f) => f && f.size >= 0);
    if (!list.length) return;
    let sent = 0;
    for (const file of list) {
      if (await sendFileToPeer(file, hashId, peerName)) sent += 1;
    }
    if (sent > 1) toast(`Sent ${sent} files`);
  }

  function setupDragDrop() {
    const contacts = $("#contacts");
    const overlay = $("#drop-overlay");
    if (!contacts) return;

    document.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer?.types?.includes("Files")) return;
      e.preventDefault();
    });

    contacts.addEventListener("dragover", (e) => {
      if (!e.dataTransfer?.types?.includes("Files")) return;
      e.preventDefault();
      const contact = e.target.closest(".contact");
      if (!contact?.dataset.hash) return;
      state.dropTargetHash = contact.dataset.hash;
      overlay?.classList.remove("hidden");
      contacts.querySelectorAll(".contact").forEach((c) => c.classList.remove("drop-target"));
      contact.classList.add("drop-target");
      e.dataTransfer.dropEffect = "copy";
    });

    contacts.addEventListener("dragleave", (e) => {
      if (e.relatedTarget && contacts.contains(e.relatedTarget)) return;
      overlay?.classList.add("hidden");
      contacts.querySelectorAll(".contact.drop-target").forEach((c) => c.classList.remove("drop-target"));
      state.dropTargetHash = null;
    });

    contacts.addEventListener("drop", async (e) => {
      e.preventDefault();
      overlay?.classList.add("hidden");
      contacts.querySelectorAll(".contact.drop-target").forEach((c) => c.classList.remove("drop-target"));
      const contact = e.target.closest(".contact");
      const hashId = contact?.dataset.hash || state.dropTargetHash;
      const peerName = contact?.dataset.name;
      state.dropTargetHash = null;
      if (!hashId || !e.dataTransfer?.files?.length) return;
      await sendDroppedFiles(e.dataTransfer.files, hashId, peerName);
    });
  }

  async function loadShareGrants() {
    try {
      const res = await fetch("/api/share/grants");
      state.shareGrants = await res.json();
    } catch (_) {
      state.shareGrants = { local: [], remote: [] };
    }
  }

  function renderShareGrants() {
    const el = $("#share-grants-list");
    if (!el) return;
    const peer = state.selectedPeer;
    const remote = (state.shareGrants.remote || []).filter(
      (g) => !peer || g.owner_hash === peer
    );
    const local = (state.shareGrants.local || []).filter(
      (g) => !peer || g.recipient_hash === peer
    );
    let html = "";
    if (state.shareMode === "offer" && peer && isPeerLinked(peer)) {
      const folder = state.settings.shared_folder || "";
      $("#share-offer-options")?.classList.remove("hidden");
      html += `<div class="share-section">
        <p class="share-section-title">Offer to ${escapeHtml(state.selectedName || "peer")}</p>
        <p class="share-hint">Folder: ${escapeHtml(folder || "(default shared folder)")}</p>
        <button type="button" class="action-btn" id="share-offer-btn">Offer shared folder (E2EE)</button>
      </div>`;
    } else {
      $("#share-offer-options")?.classList.add("hidden");
    }
    if (remote.length) {
      html += `<div class="share-section"><p class="share-section-title">Available from peers</p>`;
      html += remote.map((g) => {
        const owner = peerByHash(g.owner_hash);
        return `<button type="button" class="share-grant-btn" data-owner="${escapeHtml(g.owner_hash)}" data-grant="${escapeHtml(g.grant_id)}">
          ${escapeHtml(g.label || "shared")} · ${escapeHtml(owner?.name || g.owner_hash.slice(0, 8))}
        </button>`;
      }).join("");
      html += "</div>";
    }
    if (local.length) {
      html += `<div class="share-section"><p class="share-section-title">Your active offers</p>`;
      html += local.map((g) => {
        const recip = peerByHash(g.recipient_hash);
        const limit = g.max_downloads
          ? `${g.download_count || 0}/${g.max_downloads} downloads`
          : "unlimited downloads";
        return `<div class="share-grant-item">
          <span>${escapeHtml(g.label)} → ${escapeHtml(recip?.name || g.recipient_hash.slice(0, 8))}</span>
          <span class="share-grant-meta">${escapeHtml(limit)}</span>
          <button type="button" class="share-revoke-btn" data-grant="${escapeHtml(g.grant_id)}">Remove</button>
        </div>`;
      }).join("");
      html += "</div>";
    }
    if (!html) {
      html = '<div class="empty-hint">No shared folders yet. Connect to a trusted peer and offer a folder.</div>';
    }
    el.innerHTML = html;
    $("#share-offer-btn")?.addEventListener("click", offerShareToSelected);
    el.querySelectorAll(".share-grant-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        requestShareListing(btn.dataset.owner, btn.dataset.grant);
      });
    });
    el.querySelectorAll(".share-revoke-btn").forEach((btn) => {
      btn.addEventListener("click", () => revokeShareGrant(btn.dataset.grant));
    });
  }

  function renderShareEntries() {
    const el = $("#share-entries");
    if (!el) return;
    const entries = state.shareListing.entries || [];
    if (!entries.length) {
      el.innerHTML = '<div class="empty-hint">Folder is empty or listing pending…</div>';
      return;
    }
    el.innerHTML = entries.map((e) => {
      const relPath = e.path || e.name || "";
      if (e.type === "dir") {
        return `<div class="share-entry dir">
          <span>📁 ${escapeHtml(e.name)}</span>
          <button type="button" class="share-entry-dl" data-path="${escapeHtml(relPath)}" data-folder="1">Download as ZIP</button>
        </div>`;
      }
      return `<button type="button" class="share-entry file" data-path="${escapeHtml(relPath)}">
        📄 ${escapeHtml(e.name)} <span class="share-size">${formatBytes(e.size || 0)}</span>
      </button>`;
    }).join("");
    el.querySelectorAll(".share-entry.file").forEach((btn) => {
      btn.addEventListener("click", () => fetchShareFile(btn.dataset.path, false));
    });
    el.querySelectorAll(".share-entry-dl").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        fetchShareFile(btn.dataset.path, true);
      });
    });
  }

  async function openShareModal(mode = "browse") {
    state.shareMode = mode;
    state.shareListing = { ownerHash: null, grantId: null, entries: [] };
    await loadShareGrants();
    renderShareGrants();
    renderShareEntries();
    $("#share-modal")?.classList.add("open");
  }

  function closeShareModal() {
    $("#share-modal")?.classList.remove("open");
  }

  async function offerShareToSelected() {
    if (!state.selectedPeer) {
      toast("Select a peer first");
      return;
    }
    if (!isPeerLinked(state.selectedPeer)) {
      toast("Connect to peer before sharing");
      await connectPeer(state.selectedPeer, false);
      if (!isPeerLinked(state.selectedPeer)) return;
    }
    const res = await fetch("/api/share/peer/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recipient_hash: state.selectedPeer,
        label: state.settings.shared_folder?.split("/").pop() || "shared",
        ttl_preset: $("#share-ttl")?.value || "1h",
        download_limit_preset: $("#share-download-limit")?.value || "unlimited",
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "Share offer failed");
      return;
    }
    toast("Shared folder offered (E2EE)");
    await loadShareGrants();
    renderShareGrants();
  }

  async function requestShareListing(ownerHash, grantId) {
    state.shareListing = { ownerHash, grantId, entries: [] };
    renderShareEntries();
    toast("Loading folder listing…");
    const res = await fetch("/api/share/peer/list", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner_hash: ownerHash, grant_id: grantId }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "Could not list folder");
      return;
    }
    if (Array.isArray(data.entries)) {
      state.shareListing = {
        ownerHash: data.owner_hash || ownerHash,
        grantId: data.grant_id || grantId,
        entries: data.entries,
      };
      renderShareEntries();
      if (data.error) {
        toast(data.error, "warning");
      } else if (!data.entries.length) {
        toast("Folder is empty");
      } else {
        toast(`Listed ${data.entries.length} item${data.entries.length === 1 ? "" : "s"}`);
      }
    }
  }

  async function fetchShareFile(relPath, asFolder = false) {
    const { ownerHash, grantId } = state.shareListing;
    if (!ownerHash || !grantId || relPath == null) return;
    const res = await fetch("/api/share/peer/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        owner_hash: ownerHash,
        grant_id: grantId,
        path: relPath,
        as_folder: asFolder,
      }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      toast(data.error || "Download request failed");
      return;
    }
    toast(asFolder ? "Folder ZIP transfer started — check chat" : "File transfer started — check chat");
    if (state.selectedPeer === ownerHash) {
      setTimeout(loadMessages, 800);
    }
  }

  async function revokeShareGrant(grantId) {
    if (!grantId || !confirm("Remove this shared folder offer? The peer will lose access.")) return;
    const res = await fetch("/api/share/peer/revoke", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ grant_id: grantId }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "Could not revoke share");
      return;
    }
    toast("Share removed");
    await loadShareGrants();
    renderShareGrants();
  }

  function openWanModal(hashId) {
    const peer = state.trusted.find((p) => p.hash_id === hashId);
    if (!peer) return;
    state.wanModalTarget = hashId;
    $("#wan-host").value = peer.wan_host || "";
    $("#wan-port").value = peer.wan_port || 7825;
    $("#wan-enabled").checked = !!peer.wan_enabled;
    $("#wan-mode").value = peer.connection_mode || "auto";
    $("#wan-modal")?.classList.add("open");
    closeContactMenu();
  }

  function closeWanModal() {
    $("#wan-modal")?.classList.remove("open");
    state.wanModalTarget = null;
  }

  async function saveWanSettings() {
    const hashId = state.wanModalTarget;
    if (!hashId) return;
    const body = {
      wan_host: $("#wan-host").value.trim(),
      wan_port: parseInt($("#wan-port").value, 10) || 7825,
      wan_enabled: $("#wan-enabled").checked,
      connection_mode: $("#wan-mode").value,
    };
    const res = await fetch(`/api/trusted/${encodeURIComponent(hashId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "Failed to save WAN settings");
      return;
    }
    toast("WAN endpoint saved");
    closeWanModal();
    loadPeers();
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
    if ($("#set-tcp-port")) $("#set-tcp-port").value = settings.tcp_port || 7825;
    if ($("#set-discovery-port")) {
      $("#set-discovery-port").value = settings.discovery_port || 7826;
    }
    if ($("#set-strict-ports")) {
      $("#set-strict-ports").checked = settings.strict_ports !== false;
    }
    const preset = settings.message_retention_preset || "1w";
    if ($("#set-retention")) $("#set-retention").value = preset;
    $("#set-incoming").value = settings.incoming_files_dir || "";
    $("#set-shared").value = settings.shared_folder || "";
    $("#set-auto-announce").checked = !!settings.auto_announce;
    if ($("#set-wan-expose")) $("#set-wan-expose").checked = !!settings.wan_expose_port;
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
    const prev = { ...state.settings };
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
    const portsChanged = ["web_port", "tcp_port", "discovery_port", "strict_ports"].some(
      (k) => prev[k] !== state.settings[k]
    );
    if (portsChanged) {
      toast("Settings saved — restart SRLTCP to apply port changes");
    } else {
      toast(complete ? "Setup complete!" : "Settings saved");
    }
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
    if (isPeerLinked(peer.hash_id)) return "Connected";
    return "Offline";
  }

  function stopNetworkAnimation() {
    if (networkAnimFrame) {
      cancelAnimationFrame(networkAnimFrame);
      networkAnimFrame = null;
    }
  }

  function layoutNetworkNodes(nodes, w, h) {
    const centerX = w / 2;
    const centerY = h / 2;
    const radius = Math.min(w, h) * 0.36;
    const positions = {};
    const selfNodes = nodes.filter((n) => n.role === "self");
    selfNodes.forEach((n, idx) => {
      const angle = (idx / Math.max(selfNodes.length, 1)) * Math.PI * 2 - Math.PI / 2;
      positions[n.id] = {
        x: centerX + Math.cos(angle) * 52,
        y: centerY + Math.sin(angle) * 52,
        r: 20,
      };
    });
    const others = nodes.filter((n) => n.role !== "self");
    others.forEach((n, i) => {
      const angle = (i / Math.max(others.length, 1)) * Math.PI * 2 - Math.PI / 2;
      positions[n.id] = {
        x: centerX + Math.cos(angle) * radius,
        y: centerY + Math.sin(angle) * radius,
        r: 15,
      };
    });
    return positions;
  }

  function drawNetworkFrame(canvas, data, tick) {
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    const nodes = data.nodes || [];
    const edges = data.edges || [];
    const positions = layoutNetworkNodes(nodes, w, h);

    const grad = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, w * 0.55);
    grad.addColorStop(0, "#141824");
    grad.addColorStop(1, "#0a0c12");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, h);

    ctx.strokeStyle = "rgba(91, 141, 239, 0.06)";
    ctx.lineWidth = 1;
    for (let x = 40; x < w; x += 40) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
    }
    for (let y = 40; y < h; y += 40) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    edges.forEach((e, i) => {
      const a = positions[e.from];
      const b = positions[e.to];
      if (!a || !b) return;
      const discovered = e.state === "discovered";
      const linked = e.state === "up" || e.state === "linked" || e.state === "active";
      const pulse = linked ? 0.65 + 0.35 * Math.sin(tick * 0.04 + i) : 1;
      const color = discovered
        ? "rgba(139, 149, 168, 0.45)"
        : e.transport === "serial"
          ? `rgba(62, 207, 142, ${0.55 * pulse})`
          : `rgba(91, 141, 239, ${0.7 * pulse})`;
      ctx.strokeStyle = color;
      ctx.lineWidth = linked ? 2.5 : discovered ? 1.5 : 2;
      ctx.setLineDash(discovered ? [7, 6] : []);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      if (linked) {
        const mx = (a.x + b.x) / 2;
        const my = (a.y + b.y) / 2;
        ctx.fillStyle = e.transport === "serial" ? "#3ecf8e" : "#5b8def";
        ctx.beginPath();
        ctx.arc(mx, my, 3 + Math.sin(tick * 0.06 + i) * 1.2, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.setLineDash([]);
    });

    nodes.forEach((n) => {
      const p = positions[n.id];
      if (!p) return;
      const linked = edges.some(
        (e) =>
          (e.from === n.id || e.to === n.id) &&
          (e.state === "up" || e.state === "linked" || e.state === "active")
      );
      const roleColor =
        n.role === "self" ? "#5b8def" : n.role === "trusted" ? "#3ecf8e" : "#f5a623";
      if (linked || n.role === "self") {
        const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r + 14);
        glow.addColorStop(0, `${roleColor}55`);
        glow.addColorStop(1, "transparent");
        ctx.fillStyle = glow;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r + 14, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.fillStyle = roleColor;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,0.15)";
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.fillStyle = "#e8ecf4";
      ctx.font = "600 11px DM Sans, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText((n.label || "").slice(0, 16), p.x, p.y + p.r + 16);
      const badge = (n.transport || "tcp").toUpperCase();
      ctx.fillStyle = n.transport === "serial" ? "#7dffb8" : "#8eb8ff";
      ctx.font = "500 9px JetBrains Mono, monospace";
      ctx.fillText(badge, p.x, p.y + p.r + 28);
    });
  }

  async function renderNetworkGraph() {
    const canvas = $("#network-canvas");
    if (!canvas) return;
    stopNetworkAnimation();
    const res = await fetch("/api/network");
    networkGraphData = await res.json();
    let tick = 0;
    const loop = () => {
      if (!$("#network-modal")?.classList.contains("open")) {
        stopNetworkAnimation();
        return;
      }
      drawNetworkFrame(canvas, networkGraphData, tick);
      tick += 1;
      networkAnimFrame = requestAnimationFrame(loop);
    };
    loop();
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

    const transportStatus = data.transports || {};
    state.transportStatus = transportStatus;

    const tcpBtn = $("#btn-announce-tcp");
    if (tcpBtn) {
      const tcpActive = !!transportStatus.tcp?.active;
      tcpBtn.disabled = !tcpActive;
      tcpBtn.title = tcpActive
        ? "Announce on TCP/LAN"
        : "TCP transport unavailable — restart the node";
    }

    const serialBtn = $("#btn-announce-serial");
    if (serialBtn) {
      const serialActive = !!transportStatus.serial?.active;
      serialBtn.disabled = !serialActive;
      const serialErr = transportStatus.serial?.error;
      serialBtn.title = serialActive
        ? "Announce on serial/RF"
        : serialErr
          ? serialErr
          : ids.serial
            ? "Serial port not open — Arch uses group uucp (not dialout)"
            : "Enable serial in settings first";
    }

    if (primary) {
      state.myName = primary.name;
      $("#me-name").textContent = primary.name;
      const meHash = $("#me-hash");
      if (meHash) {
        meHash.textContent = primary.hash_id;
        meHash.title = "Click to copy full hash ID";
        meHash.dataset.fullHash = primary.hash_id;
      }
      setAvatar($("#me-avatar"), primary.name, primary.hash_id);
    }

    syncLinksFromStatus(data.links || []);

    loadPeers();
    if (state.selectedPeer) refreshPeerStatus(state.selectedPeer);
    renderTransfers();
  }

  function dedupePeers(peers) {
    const seen = new Set();
    return peers.filter((p) => {
      const id = (p.hash_id || "").toLowerCase();
      if (!id || !/^[0-9a-f]{32}$/.test(id) || seen.has(id)) return false;
      seen.add(id);
      return true;
    });
  }

  function renderContacts() {
    const q = state.search.toLowerCase();
    state.trusted = dedupePeers(state.trusted);
    const trustedIds = new Set(state.trusted.map((p) => p.hash_id.toLowerCase()));
    const list = state.peerTab === "trusted"
      ? state.trusted
      : dedupePeers(state.peers).filter((p) => !trustedIds.has(p.hash_id.toLowerCase()));
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
          : "No peers yet.<br>Click <strong>Announce</strong> or use <strong>Add Contact</strong> with a peer hash ID."}
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
        const metricsStr = metrics.length ? ` · ${metrics.join(" · ")}` : "";
        const meta = `${transportBadge(p.transport)}<span class="contact-endpoint">${escapeHtml(endpoint)}${escapeHtml(metricsStr)}</span>`;
        const trustBtn = state.peerTab === "discovered" && !trustedIds.has(p.hash_id)
          ? `<button type="button" class="contact-trust" data-trust="${p.hash_id}">Trust</button>` : "";
        return `<button class="contact${active}" data-hash="${p.hash_id}" data-name="${escapeHtml(p.name)}">
          <div class="avatar" style="background:${hashColor(p.hash_id)}22;color:${hashColor(p.hash_id)};border-color:${hashColor(p.hash_id)}44">
            ${initials(p.name)}
          </div>
          <div class="contact-info">
            <div class="contact-name">${escapeHtml(p.name)}</div>
            <div class="contact-preview">${meta}</div>
          </div>
          <div class="contact-meta">${trustBtn}${linked ? '<span class="contact-online" title="Connected">●</span>' : ""}${unreadCount(p.hash_id) ? `<span class="unread-badge">${unreadCount(p.hash_id) > 99 ? "99+" : unreadCount(p.hash_id)}</span>` : ""}</div>
        </button>`;
      })
      .join("");

    el.querySelectorAll(".contact").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        if (ev.target.closest(".contact-trust")) return;
        selectPeer(btn.dataset.hash, btn.dataset.name);
        closeSidebarMobile();
      });
      btn.addEventListener("contextmenu", (ev) => {
        if (state.peerTab !== "trusted") return;
        ev.preventDefault();
        openContactMenu(btn.dataset.hash, btn.dataset.name, btn);
      });
    });
    el.querySelectorAll(".contact-trust").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        trustPeer(btn.dataset.trust);
      });
    });
  }

  function selectPeer(hashId, name) {
    state.selectedPeer = hashId;
    state.selectedName = name;
    clearUnread(hashId);

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
    if (!isPeerLinked(hashId) && !inTransferCooldown(hashId)) connectPeer(hashId, false);
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
    const downloadUrl = fileUrl ? `${fileUrl}?download=1` : "";
    const cancelled = stateLabel === "cancelled";
    const failed = stateLabel === "failed";
    const stateClass = cancelled ? " cancelled" : failed ? " failed" : "";
    const canPreview = (m.msg_type === "image" || m.msg_type === "video") && fileUrl
      && (out || offset > 0 || ["complete", "transferring", "accepted"].includes(stateLabel));
    const progressLine = stateLabel === "complete"
      ? `${formatBytes(size)} · complete`
      : `${formatBytes(offset)} / ${formatBytes(size)} · ${pct}%${speedStr}`;
    const progressBar = stateLabel !== "complete" && !cancelled && !failed
      ? `<div class="progress-track chat-progress"><div class="progress-fill" style="width:${pct}%"></div></div>`
      : "";
    const showDownload = stateLabel === "complete" && downloadUrl;
    const downloadLabel = meta.is_folder_zip ? "Download folder ZIP" : "Download file";
    const downloadLink = showDownload
      ? `<a class="file-download" href="${downloadUrl}" download="${escapeHtml(filename)}" target="_blank" rel="noopener">${downloadLabel}</a>`
      : "";

    if (canPreview && m.msg_type === "image") {
      return `<div class="file-bubble image-bubble${stateClass}" data-transfer="${escapeHtml(tid)}">
        <button type="button" class="media-preview-btn" data-media-open="${escapeHtml(fileUrl)}" data-media-kind="image" data-media-name="${escapeHtml(filename)}">
          <img src="${fileUrl}" alt="${escapeHtml(filename)}" class="chat-image" loading="lazy" />
        </button>
        <div class="file-name">${escapeHtml(filename)}</div>
        <div class="file-progress-meta">${cancelled ? "Transfer cancelled" : progressLine}</div>
        ${progressBar}
        ${downloadLink}
      </div>`;
    }

    if (canPreview && m.msg_type === "video") {
      return `<div class="file-bubble video-bubble${stateClass}" data-transfer="${escapeHtml(tid)}">
        <button type="button" class="media-preview-btn" data-media-open="${escapeHtml(fileUrl)}" data-media-kind="video" data-media-name="${escapeHtml(filename)}">
          <video src="${fileUrl}" class="chat-video" controls preload="metadata"></video>
        </button>
        <div class="file-name">${escapeHtml(filename)}</div>
        <div class="file-progress-meta">${cancelled ? "Transfer cancelled" : progressLine}</div>
        ${progressBar}
        ${downloadLink}
      </div>`;
    }

    return `<div class="file-bubble${stateClass}" data-transfer="${escapeHtml(tid)}">
      <div class="file-icon">${cancelled ? "✕" : failed ? "!" : "📎"}</div>
      <div class="file-info">
        <div class="file-name">${escapeHtml(filename)}</div>
        <div class="file-progress-meta">${
          cancelled
            ? "Transfer cancelled"
            : failed
              ? "Transfer failed"
              : stateLabel === "complete"
                ? `${formatBytes(size)} · complete`
                : `${formatBytes(offset)} / ${formatBytes(size)} · ${pct}%${speedStr}`
        }</div>
        ${progressBar}
        ${downloadLink}
      </div>
    </div>`;
  }

  function ensureTransferDownloadLink(bubble, transferId, filename) {
    if (!transferId || bubble.querySelector(".file-download")) return;
    const url = `/api/transfers/${encodeURIComponent(transferId)}/file?download=1`;
    const link = document.createElement("a");
    link.className = "file-download";
    link.href = url;
    link.setAttribute("download", filename || "file");
    link.target = "_blank";
    link.rel = "noopener";
    const msg = messageForTransfer(transferId);
    link.textContent = msg?.metadata?.is_folder_zip ? "Download folder ZIP" : "Download file";
    const info = bubble.querySelector(".file-info");
    const imageBubble = bubble.classList.contains("image-bubble") || bubble.classList.contains("video-bubble");
    if (info) {
      info.appendChild(link);
    } else if (imageBubble) {
      bubble.appendChild(link);
    }
  }

  async function copyMessageText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied to clipboard");
    } catch (_) {
      toast("Copy failed");
    }
  }

  async function deleteMessage(messageId) {
    if (!messageId) return;
    const res = await fetch(`/api/messages/${encodeURIComponent(messageId)}`, { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (data.deleted) {
      state.messageCache = state.messageCache.filter((m) => m.id !== messageId);
      renderMessages(state.messageCache);
      toast("Message deleted");
    } else {
      toast("Could not delete message");
    }
  }

  function isNearBottom(el, threshold = 80) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
  }

  function scrollMessagesToBottom() {
    const el = $("#messages");
    if (el) el.scrollTop = el.scrollHeight;
  }

  function messageForTransfer(transferId) {
    return state.messageCache.find((m) => m.metadata?.transfer_id === transferId);
  }

  function syncTransferMessage(data) {
    if (!data?.id) return;
    const idx = state.messageCache.findIndex((m) => m.metadata?.transfer_id === data.id);
    if (idx < 0) return;
    const meta = state.messageCache[idx].metadata || {};
    state.messageCache[idx].metadata = {
      ...meta,
      transfer_id: data.id,
      state: data.state ?? meta.state,
      offset: data.offset ?? meta.offset,
      size: data.size ?? meta.size,
      speed_mbps: data.speed_mbps ?? meta.speed_mbps,
      filename: data.filename ?? meta.filename,
      is_folder_zip: data.metadata?.is_folder_zip ?? meta.is_folder_zip,
      folder_name: data.metadata?.folder_name ?? meta.folder_name,
    };
  }

  function bubbleNeedsMediaRender(msg, bubble, data) {
    if (!msg || !bubble) return false;
    if (msg.msg_type !== "image" && msg.msg_type !== "video") return false;
    const stateLabel = data.state || "";
    const hasMediaShell = bubble.classList.contains("image-bubble")
      || bubble.classList.contains("video-bubble");
    if (stateLabel === "complete" && !hasMediaShell) return true;
    if (stateLabel === "transferring" && (data.offset || 0) > 0 && !hasMediaShell) return true;
    return false;
  }

  function refreshTransferBubble(transferId, data, { scroll = false } = {}) {
    const bubble = document.querySelector(`[data-transfer="${CSS.escape(transferId)}"]`);
    const msg = messageForTransfer(transferId);
    if (bubbleNeedsMediaRender(msg, bubble, data)) {
      renderMessages(state.messageCache, { scrollToBottom: scroll });
      return;
    }
    if (!patchTransferBubble(transferId, data)) {
      renderMessages(state.messageCache, { scrollToBottom: scroll });
      return;
    }
    if (scroll && msg && (msg.msg_type === "image" || msg.msg_type === "video") && data.state === "complete") {
      scrollMessagesToBottom();
    }
  }

  function patchTransferBubble(transferId, data) {
    const bubble = document.querySelector(`[data-transfer="${CSS.escape(transferId)}"]`);
    if (!bubble) return false;
    const msg = messageForTransfer(transferId);
    if (bubbleNeedsMediaRender(msg, bubble, data)) return false;
    const size = data.size || 0;
    const offset = data.offset || 0;
    const pct = size ? Math.min(100, Math.round((offset / size) * 100)) : 0;
    const stateLabel = data.state || "transferring";
    const speed = data.speed_mbps ? ` · ${Number(data.speed_mbps).toFixed(2)} MB/s` : "";
    const cancelled = stateLabel === "cancelled";
    const failed = stateLabel === "failed";
    const meta = bubble.querySelector(".file-progress-meta");
    if (meta) {
      meta.textContent = cancelled
        ? "Transfer cancelled"
        : stateLabel === "complete"
          ? `${formatBytes(size)} · complete`
          : `${formatBytes(offset)} / ${formatBytes(size)} · ${pct}%${speed}`;
    }
    if (stateLabel === "complete" || cancelled || failed) {
      bubble.querySelectorAll(".progress-track, .chat-progress").forEach((el) => el.remove());
      bubble.classList.remove("cancelled", "failed");
      if (cancelled) bubble.classList.add("cancelled");
      if (failed) bubble.classList.add("failed");
      if (stateLabel === "complete") {
        const filename = bubble.querySelector(".file-name")?.textContent || data.filename || "file";
        ensureTransferDownloadLink(bubble, transferId, filename);
      }
    } else {
      bubble.querySelectorAll(".progress-fill").forEach((fill) => {
        fill.style.width = `${pct}%`;
      });
    }
    return true;
  }

  function scheduleTransferPatch(transferId, data) {
    state.pendingTransferPatches.set(transferId, data);
    if (state.transferPatchTimer) return;
    state.transferPatchTimer = setTimeout(() => {
      state.transferPatchTimer = null;
      state.pendingTransferPatches.forEach((patchData, tid) => {
        refreshTransferBubble(tid, patchData, { scroll: false });
      });
      state.pendingTransferPatches.clear();
    }, 120);
  }

  function renderMessages(msgs, { preserveScroll = false, scrollToBottom = false } = {}) {
    const el = $("#messages");
    const wasAtBottom = scrollToBottom || (preserveScroll ? isNearBottom(el) : true);
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
      const isFile = m.msg_type === "file" || m.msg_type === "image" || m.msg_type === "video";
      const body = isFile ? renderFileBubble(m, out) : escapeHtml(m.text);
      const actions = isFile
        ? ""
        : `<div class="bubble-actions">
            <button type="button" class="bubble-action icon-only" data-copy="${escapeHtml(m.id)}" title="Copy" aria-label="Copy">${ICON_COPY}</button>
            <button type="button" class="bubble-action icon-only danger" data-del-msg="${escapeHtml(m.id)}" title="Delete" aria-label="Delete">${ICON_TRASH}</button>
          </div>`;
      html += `<div class="bubble-row ${out ? "out" : "in"}" data-msg-id="${escapeHtml(m.id)}">
        <div class="bubble ${out ? "out" : "in"} ${m.msg_type === "image" ? "image" : ""}">
          ${actions}
          ${body}
          <div class="bubble-meta">
            <span>${formatTime(m.timestamp)}</span>
            <span class="bubble-status ${m.status}"></span>
          </div>
        </div>
      </div>`;
    });

    el.innerHTML = html || '<div class="empty-hint" style="text-align:center;padding:2rem">No messages yet — say hello!</div>';
    el.querySelectorAll("[data-copy]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const msg = state.messageCache.find((m) => m.id === btn.dataset.copy);
        if (msg?.text) copyMessageText(msg.text);
      });
    });
    el.querySelectorAll("[data-del-msg]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        deleteMessage(btn.dataset.delMsg);
      });
    });
    el.querySelectorAll("[data-media-open]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        openMediaLightbox(
          btn.dataset.mediaOpen,
          btn.dataset.mediaKind,
          btn.dataset.mediaName
        );
      });
    });
    if (wasAtBottom) el.scrollTop = el.scrollHeight;
  }

  function applyMediaZoom() {
    const body = $("#media-lightbox-body");
    const level = $("#media-zoom-level");
    const media = body?.querySelector(".lightbox-media");
    if (!media) return;
    const z = state.mediaZoom;
    const { x, y } = state.mediaPan;
    media.style.transform = `translate(${x}px, ${y}px) scale(${z})`;
    if (level) level.textContent = `${Math.round(z * 100)}%`;
  }

  function setMediaZoom(delta) {
    state.mediaZoom = Math.min(4, Math.max(0.25, state.mediaZoom + delta));
    applyMediaZoom();
  }

  function resetMediaZoom() {
    state.mediaZoom = 1;
    state.mediaPan = { x: 0, y: 0 };
    applyMediaZoom();
  }

  function setupMediaPan() {
    const body = $("#media-lightbox-body");
    if (!body) return;
    body.addEventListener("mousedown", (e) => {
      if (!$("#media-lightbox")?.classList.contains("open")) return;
      if (e.button !== 0) return;
      state.mediaDragging = true;
      state.mediaDragStart = { x: e.clientX - state.mediaPan.x, y: e.clientY - state.mediaPan.y };
      body.style.cursor = "grabbing";
    });
    window.addEventListener("mousemove", (e) => {
      if (!state.mediaDragging || !state.mediaDragStart) return;
      state.mediaPan = {
        x: e.clientX - state.mediaDragStart.x,
        y: e.clientY - state.mediaDragStart.y,
      };
      applyMediaZoom();
    });
    window.addEventListener("mouseup", () => {
      state.mediaDragging = false;
      state.mediaDragStart = null;
      if (body) body.style.cursor = "grab";
    });
  }

  function openMediaLightbox(url, kind, filename) {
    const modal = $("#media-lightbox");
    const body = $("#media-lightbox-body");
    const dl = $("#media-lightbox-download");
    if (!modal || !body) {
      window.open(url, "_blank", "noopener");
      return;
    }
    const downloadUrl = `${url}${url.includes("?") ? "&" : "?"}download=1`;
    state.mediaZoom = 1;
    state.mediaPan = { x: 0, y: 0 };
    if (kind === "video") {
      body.innerHTML = `<video src="${url}" controls autoplay class="lightbox-video lightbox-media"></video>`;
    } else {
      body.innerHTML = `<img src="${url}" alt="${escapeHtml(filename || "")}" class="lightbox-image lightbox-media" />`;
    }
    if (dl) {
      dl.href = downloadUrl;
      dl.setAttribute("download", filename || "");
    }
    applyMediaZoom();
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
  }

  function closeMediaLightbox() {
    const modal = $("#media-lightbox");
    const body = $("#media-lightbox-body");
    if (!modal) return;
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    if (body) body.innerHTML = "";
    state.mediaZoom = 1;
  }

  function updateChatTransfer(data) {
    if (!data.id) return;
    state.transfers[data.id] = { ...state.transfers[data.id], ...data };
    const peer = state.selectedPeer;
    if (!peer) return;
    const relevant = data.sender_hash === peer || data.recipient_hash === peer;
    if (!relevant) return;
    const idx = state.messageCache.findIndex((m) => m.metadata?.transfer_id === data.id);
    if (idx >= 0) {
      const meta = state.messageCache[idx].metadata || {};
      state.messageCache[idx].metadata = {
        ...meta,
        transfer_id: data.id || meta.transfer_id,
        state: data.state ?? meta.state,
        offset: data.offset ?? meta.offset,
        size: data.size ?? meta.size,
        speed_mbps: data.speed_mbps ?? meta.speed_mbps,
        filename: data.filename ?? meta.filename,
      };
      if (["complete", "failed", "cancelled"].includes(data.state)) {
        refreshTransferBubble(data.id, data, { scroll: data.state === "complete" });
        if (data.state === "complete") hideTransferDockIfDone(data.id);
        return;
      }
      scheduleTransferPatch(data.id, data);
    } else {
      loadMessages();
    }
  }

  function onNewMessage(m) {
    const forPeer = state.selectedPeer
      && (m.sender_hash === state.selectedPeer || m.recipient_hash === state.selectedPeer);

    if (!forPeer) {
      if (!isOutgoing(m.sender_hash)) {
        bumpUnread(m.sender_hash);
        const sender = peerByHash(m.sender_hash);
        notifyUser(
          sender?.name || "New message",
          m.msg_type === "text" ? (m.text || "").slice(0, 120) : `Sent a ${m.msg_type}`,
          { tag: `msg-${m.sender_hash}` }
        );
      }
      return;
    }

    if (m.msg_type === "file" || m.msg_type === "image" || m.msg_type === "video") {
      const tid = m.metadata?.transfer_id;
      const idx = state.messageCache.findIndex((x) => x.id === m.id);
      if (idx >= 0) {
        const prev = state.messageCache[idx];
        state.messageCache[idx] = m;
        if (tid) {
          state.transfers[tid] = { ...state.transfers[tid], ...m.metadata, id: tid };
          if (patchTransferBubble(tid, state.transfers[tid])) return;
        }
        if (prev.metadata?.state === m.metadata?.state
            && prev.metadata?.offset === m.metadata?.offset) return;
        renderMessages(state.messageCache, { preserveScroll: true });
      } else {
        loadMessages();
      }
      return;
    }

    const idx = state.messageCache.findIndex((x) => x.id === m.id);
    if (idx >= 0) {
      state.messageCache[idx] = m;
      renderMessages(state.messageCache, { scrollToBottom: true });
    } else {
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

  function hasActiveTransfers() {
    return Object.values(state.transfers).some((t) => ACTIVE_TRANSFER_STATES.has(t.state));
  }

  function closeTransferDock() {
    const dock = $("#transfer-dock");
    if (!dock) return;
    dock.classList.add("hidden");
    delete dock.dataset.transferId;
  }

  function updateTransferDock(data) {
    const dock = $("#transfer-dock");
    if (!dock || !data) return;
    if (data.id && state.finishedTransferIds.has(data.id)) {
      if (dock.dataset.transferId === data.id) closeTransferDock();
      return;
    }
    if (data.id) state.transfers[data.id] = { ...state.transfers[data.id], ...data };
    const active = ACTIVE_TRANSFER_STATES.has(data.state);
    const done = ["complete", "failed", "cancelled", "rejected"].includes(data.state);
    if (done) {
      if (data.id) state.finishedTransferIds.add(data.id);
      if (data.state === "cancelled") {
        toast(`Transfer cancelled: ${data.filename || "file"}`, "warning");
      } else if (data.state === "complete") {
        markTransferCooldown(data.sender_hash, data.recipient_hash);
      }
      hideTransferDockIfDone(data.id);
      return;
    }
    if (!active) return;
    dock.classList.remove("hidden");
    const pct = data.size ? Math.min(100, Math.round((data.offset / data.size) * 100)) : 0;
    const speedEl = $("#transfer-dock-speed");
    const speed = data.speed_mbps ? Number(data.speed_mbps).toFixed(2) : "0.00";
    if (speedEl) speedEl.textContent = `${speed} MB/s`;
    const fill = $("#transfer-dock-fill");
    if (fill) fill.style.width = `${pct}%`;
    dock.dataset.transferId = data.id;
  }

  function syncTransferDockVisibility() {
    const dock = $("#transfer-dock");
    if (!dock || dock.classList.contains("hidden")) return;
    const currentId = dock.dataset.transferId;
    if (!currentId) {
      closeTransferDock();
      return;
    }
    const current = state.transfers[currentId];
    if (!current || !ACTIVE_TRANSFER_STATES.has(current.state) || state.finishedTransferIds.has(currentId)) {
      closeTransferDock();
    }
  }

  function pruneCompletedTransfers() {
    Object.keys(state.transfers).forEach((id) => {
      const t = state.transfers[id];
      if (!t || !ACTIVE_TRANSFER_STATES.has(t.state)) {
        delete state.transfers[id];
      }
    });
  }

  function hideTransferDockIfDone(transferId) {
    if (transferId) {
      delete state.transfers[transferId];
      state.finishedTransferIds.add(transferId);
    }
    pruneCompletedTransfers();
    const dock = $("#transfer-dock");
    if (!transferId || dock?.dataset.transferId === transferId) {
      closeTransferDock();
    }
  }

  async function pollTransfers() {
    try {
      const res = await fetch("/api/transfers");
      const transfers = await res.json();
      const dock = $("#transfer-dock");
      const dockId = dock?.dataset.transferId;
      transfers.forEach((t) => {
        if (t.id === dockId && ACTIVE_TRANSFER_STATES.has(t.state)) {
          state.transfers[t.id] = t;
          updateTransferDock(t);
        } else if (t.id && state.finishedTransferIds.has(t.id)) {
          delete state.transfers[t.id];
        }
      });
      if (dockId && !transfers.some((t) => t.id === dockId && ACTIVE_TRANSFER_STATES.has(t.state))) {
        hideTransferDockIfDone(dockId);
      }
    } catch (_) { /* ignore */ }
  }

  async function cancelActiveTransfer() {
    const dock = $("#transfer-dock");
    const id = dock?.dataset.transferId;
    if (!id) return;
    const t = state.transfers[id];
    if (!t || !ACTIVE_TRANSFER_STATES.has(t.state)) {
      hideTransferDockIfDone(id);
      syncTransferDockVisibility();
      return;
    }
    const res = await fetch(`/api/transfers/${encodeURIComponent(id)}/cancel`, { method: "POST" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      toast(data.error || "Transfer already finished", "warning");
      hideTransferDockIfDone(id);
      return;
    }
    toast("Transfer cancelled");
    hideTransferDockIfDone(id);
    renderTransfers();
    loadMessages();
  }

  function closeSidebarMobile() {
    $("#sidebar").classList.remove("open");
  }

  /* ── Events ── */
  $("#btn-add-contact")?.addEventListener("click", openAddContactModal);
  $("#add-contact-cancel")?.addEventListener("click", closeAddContactModal);
  $("#add-contact-save")?.addEventListener("click", saveManualContact);
  $("#add-contact-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "add-contact-modal") closeAddContactModal();
  });
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

  $("#btn-send-folder-peer")?.addEventListener("click", () => {
    if (!state.selectedPeer) {
      toast("Select a peer first");
      return;
    }
    openFolderSendPicker(state.selectedPeer, state.selectedName);
  });

  $("#btn-file").addEventListener("click", () => $("#file-input").click());

  $("#file-input").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (file) sendFile(file);
    e.target.value = "";
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSettings();
    if (
      e.key === "Delete" &&
      state.peerTab === "trusted" &&
      state.selectedPeer &&
      !e.target.closest("input, textarea, select, [contenteditable='true']")
    ) {
      e.preventDefault();
      deleteTrusted(state.selectedPeer, state.selectedName);
    }
  });

  function settingsPayload(prefix) {
    return {
      display_name: $(`#${prefix}-name`).value.trim(),
      web_port: parseInt($(`#${prefix}-web-port`).value, 10),
      tcp_port: parseInt($("#set-tcp-port")?.value || "7825", 10),
      discovery_port: parseInt($("#set-discovery-port")?.value || "7826", 10),
      strict_ports: $("#set-strict-ports")?.checked !== false,
      message_retention_preset: $(`#${prefix}-retention`)?.value || "1w",
      incoming_files_dir: $(`#${prefix}-incoming`)?.value.trim() || "",
      shared_folder: $(`#${prefix}-shared`)?.value.trim() || "",
      lan_ip: $(`#${prefix}-lan-ip`)?.value || "",
      auto_announce: $(`#${prefix}-auto-announce`)?.checked || false,
      wan_expose_port: $("#set-wan-expose")?.checked || false,
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
    const params = new URLSearchParams();
    if (path) params.set("path", path);
    if (state.folderTarget === "folder-send") params.set("dirs_only", "1");
    const qs = params.toString();
    const url = qs ? `/api/browse?${qs}` : "/api/browse";
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
    if (!e.target.closest("#contact-menu")) closeContactMenu();
  });

  $("#contact-menu")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn || !state.contactMenuTarget) return;
    const { hashId, name } = state.contactMenuTarget;
    const peer = state.trusted.find((p) => p.hash_id === hashId);
    switch (btn.dataset.action) {
      case "copy-hash":
        closeContactMenu();
        await copyMessageText(hashId);
        break;
      case "send-folder":
        closeContactMenu();
        openFolderSendPicker(hashId, name);
        break;
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
      case "wan":
        openWanModal(hashId);
        break;
      case "share":
        closeContactMenu();
        if (state.selectedPeer !== hashId) selectPeer(hashId, name);
        openShareModal("browse");
        break;
      default:
        break;
    }
  });

  $("#folder-select")?.addEventListener("click", async () => {
    const path = $("#folder-crumb")?.textContent || "";
    if (state.folderTarget === "folder-send" && state.folderSendTarget) {
      const { hashId, peerName } = state.folderSendTarget;
      state.folderSendTarget = null;
      state.folderTarget = null;
      $("#folder-modal").classList.remove("open");
      await sendFolderToPeer(path, hashId, peerName);
      return;
    }
    if (state.folderTarget) $(`#${state.folderTarget}`).value = path;
    $("#folder-modal").classList.remove("open");
  });

  $("#folder-cancel")?.addEventListener("click", () => $("#folder-modal").classList.remove("open"));
  $("#release-close")?.addEventListener("click", () => $("#release-modal").classList.remove("open"));
  $("#btn-share-folder")?.addEventListener("click", () => openShareModal("offer"));
  $("#share-close")?.addEventListener("click", closeShareModal);
  $("#share-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "share-modal") closeShareModal();
  });
  $("#wan-save")?.addEventListener("click", saveWanSettings);
  $("#wan-cancel")?.addEventListener("click", closeWanModal);
  $("#wan-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "wan-modal") closeWanModal();
  });
  $("#media-lightbox-close")?.addEventListener("click", closeMediaLightbox);
  $("#media-lightbox")?.addEventListener("click", (e) => {
    if (e.target.id === "media-lightbox") closeMediaLightbox();
  });
  $("#media-zoom-in")?.addEventListener("click", () => setMediaZoom(0.25));
  $("#media-zoom-out")?.addEventListener("click", () => setMediaZoom(-0.25));
  $("#media-zoom-reset")?.addEventListener("click", resetMediaZoom);
  $("#media-lightbox-body")?.addEventListener("wheel", (e) => {
    if (!$("#media-lightbox")?.classList.contains("open")) return;
    e.preventDefault();
    setMediaZoom(e.deltaY < 0 ? 0.15 : -0.15);
  }, { passive: false });

  loadUnreadState();

  $("#btn-network-viz")?.addEventListener("click", async () => {
    $("#network-modal").classList.add("open");
    await renderNetworkGraph();
  });
  $("#network-close")?.addEventListener("click", () => {
    $("#network-modal").classList.remove("open");
    stopNetworkAnimation();
  });
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

  $("#me-hash")?.addEventListener("click", async () => {
    const hash = $("#me-hash")?.dataset.fullHash || $("#me-hash")?.textContent || "";
    if (!hash) return;
    try {
      await navigator.clipboard.writeText(hash);
      toast("Hash ID copied");
    } catch (_) {
      toast(hash);
    }
  });

  /* ── Init ── */
  setupDragDrop();
  setupMediaPan();
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
  setInterval(loadPeers, 5000);
  setInterval(pollTransfers, 2500);
  pollTransfers();

  fetch("/api/settings")
    .then((r) => r.json())
    .then((s) => {
      state.settings = s;
      showSetupIfNeeded(s);
      fillSettingsForm(s);
      applyClockVisibility();
    })
    .catch(() => {});

  fetch("/api/version")
    .then((r) => r.json())
    .then((data) => {
      if (data.version) $("#stat-version").textContent = `v${data.version}`;
    })
    .catch(() => {});

  fetch("/api/status")
    .then((r) => r.json())
    .then(renderStatus)
    .catch(() => toast("Failed to load status"));
})();