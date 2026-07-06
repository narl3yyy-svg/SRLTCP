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
    wsConnected: false,
    wsReconnectTimer: null,
    timers: [],
    pageUnloading: false,
    loadingPeers: false,
    loadingTransfers: false,
    androidView: "sidebar",

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
  const ACTIVE_TRANSFER_STATES = new Set(["transferring", "accepted", "offered", "paused"]);
  const TERMINAL_TRANSFER_STATES = new Set(["complete", "failed", "cancelled", "rejected"]);
  const DOCK_TERMINAL_STATES = TERMINAL_TRANSFER_STATES;

  function mediaMsgType(msg) {
    const t = msg?.msg_type;
    if (t === "image" || t === "video") return t;
    const name = String(msg?.metadata?.filename || msg?.text || "").toLowerCase();
    if (/\.(png|jpe?g|gif|webp|bmp|svg)$/.test(name)) return "image";
    if (/\.(mp4|webm|mov|mkv|avi|m4v|ogv)$/.test(name)) return "video";
    return t || "file";
  }

  /** Merge live WS transfer state with persisted message metadata (prefer terminal). */
  function mergeTransferMeta(tid, meta = {}) {
    const live = tid ? state.transfers[tid] : null;
    const metaState = meta.state;
    const liveState = live?.state;
    let mergedState = liveState || metaState || "transferring";
    if (TERMINAL_TRANSFER_STATES.has(metaState)) mergedState = metaState;
    else if (TERMINAL_TRANSFER_STATES.has(liveState)) mergedState = liveState;
    return {
      ...meta,
      ...(live || {}),
      transfer_id: tid || meta.transfer_id,
      state: mergedState,
      offset: live?.offset ?? meta.offset ?? 0,
      size: live?.size ?? meta.size ?? 0,
      speed_mbps: live?.speed_mbps ?? meta.speed_mbps,
      filename: live?.filename || meta.filename,
    };
  }

  function syncTransferStateFromMessage(m) {
    const tid = m?.metadata?.transfer_id;
    if (!tid) return;
    const meta = m.metadata || {};
    const prev = state.transfers[tid] || {};
    const metaState = meta.state;
    const prevState = prev.state;
    const mergedState = prevState || metaState;
    let bestState = mergedState;
    if (TERMINAL_TRANSFER_STATES.has(metaState)) bestState = metaState;
    else if (TERMINAL_TRANSFER_STATES.has(prevState)) bestState = prevState;
    state.transfers[tid] = {
      ...prev,
      ...meta,
      id: tid,
      state: bestState || "transferring",
    };
  }

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

  function debounce(fn, ms = 250) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  /* ── Modal helpers (reduces ~80 lines of repetitive open/close code) ── */
  function openModal(modalEl) {
    if (!modalEl) return;
    modalEl.classList.add("open");
    modalEl.setAttribute("aria-hidden", "false");
  }

  function closeModal(modalEl) {
    if (!modalEl) return;
    modalEl.classList.remove("open");
    modalEl.setAttribute("aria-hidden", "true");
  }

  function setupModalClose(modalEl, extraCleanup) {
    if (!modalEl) return;
    modalEl.addEventListener("click", (e) => {
      if (e.target === modalEl) {
        closeModal(modalEl);
        extraCleanup?.();
      }
    });
  }

  /* ── API helpers (reduces repetitive error toast + return blocks) ── */
  async function postOrToast(url, body, errorFallback = "Operation failed") {
    const { ok, data } = await apiPost(url, body);
    if (!ok) {
      toast(data?.error || errorFallback, "error");
      return null;
    }
    return data;
  }

  async function patchOrToast(url, body, errorFallback = "Operation failed") {
    const { ok, data } = await apiPatch(url, body);
    if (!ok) {
      toast(data?.error || errorFallback, "error");
      return null;
    }
    return data;
  }

  const JSON_HEADERS = { "Content-Type": "application/json" };

  async function safeFetch(url, options = {}, { silent = false, fallback = null } = {}) {
    try {
      const res = await fetch(url, options);
      let data = fallback;
      const ct = res.headers.get("content-type") || "";
      if (ct.includes("application/json")) {
        data = await res.json().catch(() => fallback);
      } else if (res.ok) {
        data = await res.text().catch(() => fallback);
      }
      return { ok: res.ok, status: res.status, data, response: res };
    } catch (err) {
      if (!silent) {
        console.warn("safeFetch failed:", url, err);
      }
      return { ok: false, status: 0, data: fallback, error: err };
    }
  }

  async function apiGet(url, { silent = true, fallback = null } = {}) {
    return safeFetch(url, {}, { silent, fallback });
  }

  async function apiPost(url, body, { silent = false, fallback = {} } = {}) {
    return safeFetch(
      url,
      { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) },
      { silent, fallback }
    );
  }

  async function apiPatch(url, body, { silent = false, fallback = {} } = {}) {
    return safeFetch(
      url,
      { method: "PATCH", headers: JSON_HEADERS, body: JSON.stringify(body) },
      { silent, fallback }
    );
  }

  async function apiDelete(url, { silent = false, fallback = {} } = {}) {
    return safeFetch(url, { method: "DELETE" }, { silent, fallback });
  }

  async function fetchStatus() {
    const { ok, data } = await apiGet("/api/status", { fallback: {} });
    if (ok && data) renderStatus(data);
    return data;
  }

  function setInputValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value ?? "";
  }

  function setCheckbox(id, checked) {
    const el = document.getElementById(id);
    if (el) el.checked = !!checked;
  }

  function setSelectValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value ?? "";
  }

  function statusDotEl() {
    return document.querySelector("#chat-peer-status .status-dot");
  }

  function setStatusDot(className) {
    const dot = statusDotEl();
    if (dot) dot.className = className;
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

  function pruneTransferCooldowns() {
    const now = Date.now();
    Object.keys(state.transferCooldownUntil).forEach((id) => {
      if (state.transferCooldownUntil[id] <= now) {
        delete state.transferCooldownUntil[id];
      }
    });
  }

  function inTransferCooldown(hashId) {
    pruneTransferCooldowns();
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
    const container = $("#toasts");
    if (!container) return;
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    el.setAttribute("role", type === "error" ? "alert" : "status");
    container.appendChild(el);
    setTimeout(() => el.remove(), type === "error" ? 5200 : 3600);
  }

  function notifyUser(title, body, { tag, silent = false } = {}) {
    if (!silent) toast(body, "info");
    const hidden = document.hidden;
    const androidBridge = window.SRLTCPAndroid;
    const androidBg = typeof androidBridge?.isInBackground === "function"
      && androidBridge.isInBackground();
    if (androidBridge?.showNotification && (hidden || androidBg)) {
      try {
        androidBridge.showNotification(title || "SRLTCP", body || "", tag || "");
      } catch (_) { /* ignore */ }
    }
    if (!hidden || silent) return;
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
    if (!log) return;
    const entry = document.createElement("div");
    entry.className = "entry";
    entry.textContent = `${new Date().toLocaleTimeString()} ${msg}`;
    log.prepend(entry);
    while (log.children.length > 40) log.lastChild.remove();
  }

  function setAvatar(el, name, hashId) {
    if (!el) return;
    el.textContent = initials(name);
    const color = hashColor(hashId || name);
    el.style.background = `${color}22`;
    el.style.color = color;
    el.style.borderColor = `${color}44`;
  }

  function normalizeHash(hashId) {
    return (hashId || "").toLowerCase();
  }

  function isOutgoing(senderHash) {
    const sender = normalizeHash(senderHash);
    return Object.values(state.myHashes).some((h) => normalizeHash(h) === sender);
  }

  /** True when message belongs to the currently open peer conversation. */
  function messageForSelectedPeer(m) {
    if (!state.selectedPeer || !m) return false;
    const peer = normalizeHash(state.selectedPeer);
    const sender = normalizeHash(m.sender_hash);
    const recipient = normalizeHash(m.recipient_hash);
    const mine = Object.values(state.myHashes).map(normalizeHash);
    const peerInvolved = sender === peer || recipient === peer;
    const meInvolved = mine.some((h) => h === sender || h === recipient);
    return peerInvolved && meInvolved;
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
    const label = t === "serial" ? "SERIAL" : t === "hub" ? "HUB" : "TCP";
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
  function scheduleWsReconnect() {
    if (state.pageUnloading || state.wsReconnectTimer) return;
    state.wsReconnectTimer = setTimeout(() => {
      state.wsReconnectTimer = null;
      if (!state.pageUnloading) connectWs();
    }, 3000);
  }

  function setConnectionStatus(text, online = false) {
    const el = $("#connection-status");
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("online", online);
  }

  function connectWs() {
    const host = location.host || "127.0.0.1:9876";
    if (state.ws) {
      state.ws.onclose = null;
      state.ws.onerror = null;
      try { state.ws.close(); } catch (_) { /* ignore */ }
    }
    state.ws = new WebSocket(`wss://${host}/ws`);

    state.ws.onopen = () => {
      state.wsConnected = true;
      if (state.wsReconnectTimer) {
        clearTimeout(state.wsReconnectTimer);
        state.wsReconnectTimer = null;
      }
      setConnectionStatus("Connected", true);
    };

    state.ws.onerror = () => {
      state.wsConnected = false;
    };

    state.ws.onclose = () => {
      state.wsConnected = false;
      setConnectionStatus("Reconnecting…", false);
      scheduleWsReconnect();
    };

    state.ws.onmessage = (ev) => {
      let payload;
      try {
        payload = JSON.parse(ev.data);
      } catch (_) {
        return;
      }
      const { type, data } = payload;
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
            const msgInput = $("#msg-input");
            const sendBtn = $("#send-btn");
            if (msgInput) msgInput.disabled = false;
            if (sendBtn) sendBtn.disabled = false;
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
          syncTransferMessage(data);
          if (state.selectedPeer) updateChatTransfer(data);
          renderTransferDock();
          if (type === "transfer_complete") {
            markTransferCooldown(data.sender_hash, data.recipient_hash);
            notifyUser(
              "Transfer complete",
              data.filename || "File received",
              { tag: `transfer-${data.id}` }
            );
            refreshTransferBubble(data.id, data, { scroll: !!state.selectedPeer });
          }
          if (DOCK_TERMINAL_STATES.has(data.state)) {
            setTimeout(() => {
              pruneTerminalTransfers();
              renderTransferDock();
            }, 600);
          }
          break;
        case "share_offer":
          loadShareGrants()
            .then(() => {
              if (state.selectedPeer === data.hash_id) renderShareGrants();
              toast(`Shared folder offered: ${data.label || "folder"}`);
            })
            .catch(() => toast("Failed to refresh share grants", "error"));
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
          loadShareGrants()
            .then(() => {
              renderShareGrants();
              toast("Shared folder access revoked");
            })
            .catch(() => toast("Failed to refresh share grants", "error"));
          break;
        case "transport_event":
          logActivity(`Transport: ${data.kind}${data.hash_id ? ` (${data.hash_id.slice(0, 8)})` : ""}`);
          if (data.kind === "disconnected" && data.hash_id) {
            if (inTransferCooldown(data.hash_id)) {
              if (state.selectedPeer === data.hash_id) {
                updatePeerStatus("Encrypted · Online");
                setStatusDot("status-dot online");
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
    if (state.loadingPeers) return;
    state.loadingPeers = true;
    try {
      const [pRes, tRes] = await Promise.all([
        safeFetch("/api/peers", {}, { silent: true, fallback: [] }),
        safeFetch("/api/trusted", {}, { silent: true, fallback: [] }),
      ]);
      if (pRes.ok) state.peers = pRes.data || [];
      if (tRes.ok) state.trusted = tRes.data || [];
      renderContacts();
      if (state._restorePeer) applyRestoredPeer();
    } finally {
      state.loadingPeers = false;
    }
  }

  async function trustPeer(hashId) {
    const discovered = state.peers.find((p) => p.hash_id === hashId);
    const { ok, data } = await apiPost("/api/trusted", {
      hash_id: hashId,
      transport: discovered?.transport || "tcp",
      tcp_host: discovered?.tcp_host || "",
      tcp_port: discovered?.tcp_port || 7825,
      public_key: discovered?.public_key || "",
    });
    if (!ok) {
      toast(data?.error || "Failed to trust peer", "error");
      return;
    }
    toast("Peer trusted");
    state.peers = state.peers.filter((p) => p.hash_id !== hashId);
    loadPeers();
  }

  function resetAddContactForm() {
    setInputValue("add-contact-hash", "");
    setInputValue("add-contact-name", "");
    setSelectValue("add-contact-transport", "tcp");
    setInputValue("add-contact-host", "");
    setInputValue("add-contact-port", "7825");
    setInputValue("add-contact-wan-host", "");
    setInputValue("add-contact-wan-port", "7825");
    setCheckbox("add-contact-wan-enabled", false);
    setSelectValue("add-contact-wan-mode", "auto");
  }

  function openAddContactModal() {
    resetAddContactForm();
    const modal = $("#add-contact-modal");
    openModal(modal);
    $("#add-contact-hash")?.focus();
  }

  function closeAddContactModal() {
    const modal = $("#add-contact-modal");
    closeModal(modal);
    resetAddContactForm();
  }

  async function saveManualContact() {
    const hashId = ($("#add-contact-hash")?.value || "").trim().toLowerCase();
    const name = ($("#add-contact-name")?.value || "").trim() || "Peer";
    const transport = $("#add-contact-transport")?.value || "tcp";
    const tcpHost = ($("#add-contact-host")?.value || "").trim();
    const tcpPort = parseInt($("#add-contact-port")?.value || "7825", 10);
    const wanHost = ($("#add-contact-wan-host")?.value || "").trim();
    const wanPort = parseInt($("#add-contact-wan-port")?.value || "7825", 10);
    const wanEnabled = !!$("#add-contact-wan-enabled")?.checked;
    const connectionMode = $("#add-contact-wan-mode")?.value || "auto";
    if (!/^[0-9a-f]{32}$/.test(hashId)) {
      toast("Hash ID must be exactly 32 hex characters");
      return;
    }
    const saveBtn = $("#add-contact-save");
    const saveBtnLabel = saveBtn?.textContent || "Add & trust";
    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.textContent = "Adding…";
    }
    try {
      const { ok, data } = await apiPost("/api/trusted", {
        hash_id: hashId,
        name,
        transport,
        tcp_host: tcpHost,
        tcp_port: tcpPort,
        wan_host: wanHost,
        wan_port: wanPort,
        wan_enabled: wanEnabled,
        connection_mode: connectionMode,
      });
      if (!ok) {
        toast(data?.error || "Failed to add contact", "error");
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
    } finally {
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.textContent = saveBtnLabel;
      }
    }
  }

  async function deleteTrusted(hashId, name) {
    if (!confirm(`Remove ${name} from trusted contacts?`)) return;
    closeContactMenu();
    state.trusted = state.trusted.filter((p) => p.hash_id !== hashId);
    delete state.links[hashId];
    delete state.linkMetrics[hashId];
    if (state.selectedPeer === hashId) {
      state.selectedPeer = null;
      $("#chat-active")?.classList.add("hidden");
      $("#chat-empty")?.classList.remove("hidden");
    }
    renderContacts();
    const { ok } = await apiDelete(`/api/trusted/${encodeURIComponent(hashId)}`);
    if (!ok) {
      toast("Failed to remove contact", "error");
      loadPeers();
      return;
    }
    toast("Contact removed");
    loadPeers();
  }

  async function clearChatHistory(hashId) {
    closeContactMenu();
    const { ok, data } = await apiPost(
      `/api/trusted/${encodeURIComponent(hashId)}/clear-chat`,
      {}
    );
    if (!ok) {
      toast("Failed to clear chat", "error");
      return;
    }
    toast(`Cleared ${data?.cleared || 0} message(s)`);
    if (state.selectedPeer === hashId) loadMessages();
  }

  async function renameContact(hashId, currentName) {
    closeContactMenu();
    const name = prompt("Rename contact:", currentName);
    if (!name || name.trim() === currentName) return;
    const { ok } = await apiPatch(`/api/trusted/${encodeURIComponent(hashId)}`, {
      name: name.trim(),
    });
    if (!ok) {
      toast("Rename failed", "error");
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
    const { ok } = await apiPatch(`/api/trusted/${encodeURIComponent(hashId)}`, {
      blocked: true,
    });
    if (!ok) {
      toast("Block failed", "error");
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
    const { ok, data } = await apiPost("/api/ping", { hash_id: hashId });
    if (!ok) {
      toast("Ping failed", "error");
      return;
    }
    const parts = [];
    if (data?.rtt_ms != null) parts.push(`${Math.round(data.rtt_ms)} ms`);
    if (data?.link_quality_pct != null) parts.push(`${data.link_quality_pct}% link`);
    toast(parts.length ? `Ping: ${parts.join(" · ")}` : "Ping sent");
    loadPeers();
  }

  function messagesEqual(a, b) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
      if (a[i].id !== b[i].id) return false;
      if (a[i].status !== b[i].status) return false;
      if (a[i].metadata?.state !== b[i].metadata?.state) return false;
      if (a[i].metadata?.offset !== b[i].metadata?.offset) return false;
    }
    return true;
  }

  async function loadMessages() {
    if (!state.selectedPeer) return;
    const { ok, data } = await apiGet("/api/messages?limit=500", { fallback: [] });
    if (!ok) return;
    const msgs = data || [];
    const filtered = msgs.filter((m) => messageForSelectedPeer(m));
    filtered.forEach((m) => syncTransferStateFromMessage(m));
    if (!messagesEqual(filtered, state.messageCache)) {
      renderMessages(filtered);
    }
  }

  async function announceTransport(transport) {
    const btn = transport === "tcp" ? $("#btn-announce-tcp") : $("#btn-announce-serial");
    const status = state.transportStatus?.[transport];
    if (transport === "tcp" && state.settings?.hub_enabled) {
      if (!state.transportStatus?.hub?.connected) {
        toast("Hub not connected — check hub host in Settings → Network", "error");
        return;
      }
    } else if (btn?.disabled || status && !status.active) {
      const hint = transport === "serial"
        ? "Serial port not open — enable serial in settings and check /dev permissions"
        : state.settings?.hub_enabled
          ? "Hub not connected — check hub host in Settings → Network"
          : "TCP transport unavailable — restart the node";
      toast(hint);
      return;
    }
    const { ok, data } = await safeFetch(
      `/api/announce?transport=${encodeURIComponent(transport)}`,
      { method: "POST" },
      { fallback: {} }
    );
    if (!ok) {
      toast(data?.error || `Announce ${transport.toUpperCase()} failed`, "error");
      return;
    }
    const bursts = data.bursts || 3;
    const hubMsg = state.settings?.hub_enabled && transport === "tcp";
    toast(hubMsg
      ? `Registered on hub (${bursts}× burst)`
      : `Announced on ${transport.toUpperCase()} (${bursts}× burst)`);
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
    const label = transport === "serial" ? "SERIAL" : transport === "hub" ? "HUB" : "TCP";
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
      setStatusDot("status-dot online");
    } else if (linked || inTransferCooldown(hashId)) {
      updatePeerStatus("Encrypted · Online");
      setStatusDot("status-dot online");
    } else {
      updatePeerStatus("Handshaking…");
      setStatusDot("status-dot pending");
    }
    const msgInput = $("#msg-input");
    const sendBtn = $("#send-btn");
    if (msgInput) msgInput.disabled = !hashId;
    if (sendBtn) sendBtn.disabled = !hashId;
  }

  async function waitForHandshake(hashId, maxMs = 12000) {
    const deadline = Date.now() + maxMs;
    while (Date.now() < deadline) {
      if (isPeerLinked(hashId)) return true;
      const { ok, data } = await apiGet("/api/status", { fallback: {} });
      if (ok) {
        syncLinksFromStatus(data?.links || []);
        if (isPeerLinked(hashId)) return true;
      }
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
        setStatusDot("status-dot pending");
      }
      logActivity(`Connecting to ${hashId.slice(0, 12)}…`);
      const transport = peerTransport(hashId);
      const { ok, data } = await apiPost("/api/connect", {
        hash_id: hashId,
        transport,
        force,
      });
      if (!ok && state.selectedPeer === hashId) {
        updatePeerStatus("Connection failed");
        toast(data?.error || "Could not connect to peer", "error");
        return data;
      }
      if (data?.handshake_complete) {
        state.links[hashId] = true;
        if (data.rtt_ms != null) {
          state.linkMetrics[hashId] = { rtt_ms: data.rtt_ms };
        }
        if (state.selectedPeer === hashId) refreshPeerStatus(hashId);
      } else if (data?.connected) {
        if (state.selectedPeer === hashId) {
          updatePeerStatus("Handshaking…");
          setStatusDot("status-dot pending");
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
    await apiPost("/api/disconnect", { hash_id: hashId }, { silent: true });
    delete state.links[hashId];
    delete state.linkMetrics[hashId];
    updatePeerStatus("Disconnected");
    toast("Disconnected");
    loadPeers();
  }

  async function sendMessage() {
    const input = $("#msg-input");
    if (!input) return;
    const text = input.value.trim();
    if (!text || !state.selectedPeer) return;

    input.value = "";
    autoResize(input);

    const sendBtn = $("#send-btn");
    if (sendBtn) sendBtn.disabled = true;
    const { ok } = await apiPost("/api/messages", {
      recipient_hash: state.selectedPeer,
      text,
      transport: activeLinkTransport(state.selectedPeer),
    });
    if (sendBtn) sendBtn.disabled = false;
    if (!ok) {
      toast("Message failed — reconnecting…", "error");
      await connectPeer(state.selectedPeer, true);
    }
    if (isAndroidApp() && input) {
      requestAnimationFrame(() => input.focus());
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
    const { ok: upOk, data: uploaded } = await safeFetch(
      "/api/upload",
      { method: "POST", body: form },
      { fallback: {} }
    );
    if (!upOk || !uploaded?.path) {
      toast("Upload failed", "error");
      return false;
    }
    const { ok, data: sent } = await apiPost("/api/transfer", {
      recipient_hash: hashId,
      path: uploaded.path,
      transport: activeLinkTransport(hashId),
    });
    if (!ok) {
      toast(sent?.error || "File send failed", "error");
      return false;
    }
    toast(`Sending ${file.name}…`);
    if (state.selectedPeer === hashId) loadMessages();
    renderTransferDock();
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
    const { ok, data: sent } = await apiPost("/api/transfer-folder", {
      recipient_hash: hashId,
      path: folderPath,
      transport: activeLinkTransport(hashId),
    });
    if (!ok) {
      toast(sent?.error || "Folder send failed", "error");
      return false;
    }
    toast(`Sending folder ${folderName}.zip…`);
    if (state.selectedPeer === hashId) loadMessages();
    renderTransferDock();
    renderTransfers();
    return true;
  }

  function openFolderSendPicker(hashId, peerName) {
    state.folderSendTarget = { hashId, peerName };
    state.folderTarget = "folder-send";
    browseFolder(null);
    openModal($("#folder-modal"));
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
      try {
        await sendDroppedFiles(e.dataTransfer.files, hashId, peerName);
      } catch (err) {
        console.warn("drop send failed:", err);
        toast("File drop send failed", "error");
      }
    });

    const messages = $("#messages");
    if (messages) {
      messages.addEventListener("dragover", (e) => {
        if (!e.dataTransfer?.types?.includes("Files") || !state.selectedPeer) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      });
      messages.addEventListener("drop", async (e) => {
        if (!state.selectedPeer || !e.dataTransfer?.files?.length) return;
        e.preventDefault();
        try {
          await sendDroppedFiles(
            e.dataTransfer.files,
            state.selectedPeer,
            state.selectedName
          );
        } catch (err) {
          console.warn("message drop failed:", err);
          toast("File drop send failed", "error");
        }
      });
    }
  }

  async function loadShareGrants() {
    const { ok, data } = await apiGet("/api/share/grants", {
      fallback: { local: [], remote: [] },
    });
    state.shareGrants = ok ? (data || { local: [], remote: [] }) : { local: [], remote: [] };
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
    openModal($("#share-modal"));
  }

  function closeShareModal() {
    closeModal($("#share-modal"));
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
    const { ok, data } = await apiPost("/api/share/peer/offer", {
      recipient_hash: state.selectedPeer,
      label: state.settings.shared_folder?.split("/").pop() || "shared",
      ttl_preset: $("#share-ttl")?.value || "1h",
      download_limit_preset: $("#share-download-limit")?.value || "unlimited",
    });
    if (!ok) {
      toast(data?.error || "Share offer failed", "error");
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
    const { ok, data } = await apiPost("/api/share/peer/list", {
      owner_hash: ownerHash,
      grant_id: grantId,
    });
    if (!ok) {
      toast(data?.error || "Could not list folder", "error");
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
    const { ok, data } = await apiPost("/api/share/peer/fetch", {
      owner_hash: ownerHash,
      grant_id: grantId,
      path: relPath,
      as_folder: asFolder,
    });
    if (!ok) {
      toast(data?.error || "Download request failed", "error");
      return;
    }
    toast(asFolder ? "Folder ZIP transfer started — check chat" : "File transfer started — check chat");
    if (state.selectedPeer === ownerHash) {
      setTimeout(loadMessages, 800);
    }
  }

  async function revokeShareGrant(grantId) {
    if (!grantId || !confirm("Remove this shared folder offer? The peer will lose access.")) return;
    const { ok, data } = await apiPost("/api/share/peer/revoke", { grant_id: grantId });
    if (!ok) {
      toast(data?.error || "Could not revoke share", "error");
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
    setInputValue("wan-host", peer.wan_host || "");
    setInputValue("wan-port", String(peer.wan_port || 7825));
    setCheckbox("wan-enabled", !!peer.wan_enabled);
    setSelectValue("wan-mode", peer.connection_mode || "auto");
    openModal($("#wan-modal"));
    closeContactMenu();
  }

  function closeWanModal() {
    closeModal($("#wan-modal"));
    state.wanModalTarget = null;
  }

  async function saveWanSettings() {
    const hashId = state.wanModalTarget;
    if (!hashId) return;
    const body = {
      wan_host: ($("#wan-host")?.value || "").trim(),
      wan_port: parseInt($("#wan-port")?.value || "7825", 10) || 7825,
      wan_enabled: !!$("#wan-enabled")?.checked,
      connection_mode: $("#wan-mode")?.value || "auto",
    };
    const { ok, data } = await apiPatch(`/api/trusted/${encodeURIComponent(hashId)}`, body);
    if (!ok) {
      toast(data?.error || "Failed to save WAN settings", "error");
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
        apiGet("/api/serial/ports", { fallback: { ports: [], group: {} } }),
        apiGet("/api/serial/baud-rates", { fallback: { rates: [115200] } }),
      ]);
      const portsData = portsRes.data || { ports: [], group: {} };
      const baudData = baudRes.data || { rates: [115200] };
      const ports = portsData.ports || [];
      const group = portsData.group || {};
      portEl.innerHTML = ports.length
        ? ports.map((p) => {
            const access = p.accessible ? "" : " — no permission";
            return `<option value="${escapeHtml(p.device)}" ${p.device === selectedPort ? "selected" : ""}>${escapeHtml(p.description)}${access}</option>`;
          }).join("")
        : `<option value="">No serial devices detected</option>`;
      if (selectedPort && !ports.some((p) => p.device === selectedPort)) {
        portEl.innerHTML += `<option value="${escapeHtml(selectedPort)}" selected>${escapeHtml(selectedPort)} (saved)</option>`;
      }
      const hintEl = $("#set-serial-hint");
      if (hintEl) {
        if (group.needs_relogin && group.group) {
          hintEl.textContent = `You are in '${group.group}' but this session has not picked it up — restart via ./run.sh web or log out/in.`;
        } else if (group.group && !group.in_session && !group.in_account) {
          hintEl.textContent = `Add your user to '${group.group}' (Arch: uucp, Debian/Ubuntu: dialout), log out/in, then restart SRLTCP.`;
        } else if (ports.length && ports.every((p) => !p.accessible)) {
          hintEl.textContent = `Serial device(s) detected but not readable — check ${group.group || "uucp/dialout"} group permissions.`;
        } else {
          hintEl.innerHTML = "Both peers need serial enabled and the port open to send/receive RF announces.";
        }
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
    if (!selectEl) return;
    const { ok, data } = await apiGet("/api/interfaces", { fallback: { interfaces: [] } });
    state.interfaces = ok ? (data?.interfaces || []) : [];
    selectEl.innerHTML = state.interfaces
      .map((i) => `<option value="${escapeHtml(i.ip)}" ${i.ip === selectedIp ? "selected" : ""}>${escapeHtml(i.label)}</option>`)
      .join("");
    if (!state.interfaces.length) {
      selectEl.innerHTML = '<option value="">127.0.0.1</option>';
    }
  }

  function fillSettingsForm(settings) {
    if (!settings) return;
    setInputValue("set-name", settings.display_name || "");
    setInputValue("set-web-port", String(settings.web_port || 9876));
    setInputValue("set-tcp-port", String(settings.tcp_port || 7825));
    setInputValue("set-discovery-port", String(settings.discovery_port || 7826));
    setCheckbox("set-strict-ports", settings.strict_ports !== false);
    setSelectValue("set-retention", settings.message_retention_preset || "1w");
    setInputValue("set-incoming", settings.incoming_files_dir || "");
    setInputValue("set-shared", settings.shared_folder || "");
    setCheckbox("set-auto-announce", !!settings.auto_announce);
    setCheckbox("set-hub-enabled", !!settings.hub_enabled);
    setInputValue("set-hub-host", settings.hub_host || "");
    setInputValue("set-hub-lan-host", settings.hub_lan_host || "");
    setInputValue("set-hub-port", String(settings.hub_port || 7825));
    setCheckbox("set-wan-expose", !!settings.wan_expose_port);
    setSelectValue("set-handshake-protocol", settings.handshake_protocol || "identity");
    setCheckbox("set-enable-serial", !!settings.enable_serial);
    loadSerialSettings(settings.serial_port || "", settings.serial_baud || 57600);
    const tzEl = $("#set-timezone");
    if (tzEl) loadTimezones(tzEl, settings.timezone || "");
    setCheckbox("set-show-clock", settings.show_clock !== false);
    setSelectValue("set-clock-source", settings.clock_source || "system");
    setInputValue("set-ntp-server", settings.ntp_server || "pool.ntp.org");
    toggleNtpField();
    applyClockVisibility();
    if (!$("#settings-window")?.classList.contains("hidden") || !state.interfacesLoaded) {
      loadInterfaces($("#set-lan-ip"), settings.lan_ip || "");
      state.interfacesLoaded = true;
    }
  }

  async function saveSettings(formData, complete) {
    const prev = { ...state.settings };
    const { ok, data } = await apiPost("/api/settings", {
      ...formData,
      setup_complete: complete,
    });
    if (!ok) {
      toast("Failed to save settings", "error");
      return false;
    }
    state.settings = data || state.settings;
    const serialStatus = state.settings.transports?.serial;
    if (serialStatus) {
      state.transportStatus = state.settings.transports;
      delete state.settings.transports;
    }
    const portsChanged = ["web_port", "tcp_port", "discovery_port", "strict_ports"].some(
      (k) => prev[k] !== state.settings[k]
    );
    if (portsChanged) {
      toast("Settings saved — restart SRLTCP to apply port changes");
    } else if (formData.enable_serial && serialStatus && !serialStatus.active) {
      toast(serialStatus.error || "Serial port could not be opened — check permissions");
    } else {
      toast(complete ? "Setup complete!" : "Settings saved");
    }
    if (complete) $("#setup-overlay")?.classList.add("hidden");
    await fetchStatus();
    return true;
  }

  function showSetupIfNeeded(settings) {
    if (!settings || settings.setup_complete) return;
    $("#setup-overlay")?.classList.remove("hidden");
    setInputValue("setup-name", settings.display_name || "");
    setInputValue("setup-web-port", String(settings.web_port || 9876));
    setSelectValue("setup-retention", settings.message_retention_preset || "1w");
    setCheckbox("setup-auto-announce", !!settings.auto_announce);
    loadInterfaces($("#setup-lan-ip"), settings.lan_ip || "");
  }

  async function loadTimezones(selectEl, selectedTz) {
    if (!selectEl) return;
    if (!state.timezones.length) {
      const { ok, data } = await apiGet("/api/timezones", { fallback: { timezones: ["UTC"] } });
      state.timezones = ok ? (data?.timezones || ["UTC"]) : ["UTC"];
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

  function isAndroidApp() {
    return document.documentElement.classList.contains("android-app") ||
      state.settings?.platform === "android";
  }

  async function deleteSettingsFolder(inputId) {
    const input = $(`#${inputId}`);
    const folderPath = input?.value?.trim();
    if (!folderPath) {
      toast("No folder selected", "error");
      return;
    }
    if (!confirm(`Delete folder on device?\n\n${folderPath}\n\nThis cannot be undone.`)) return;
    const { ok, data } = await apiPost("/api/folders/delete", { path: folderPath }, { fallback: {} });
    if (!ok) {
      toast(data?.error || "Could not delete folder", "error");
      return;
    }
    if (input) input.value = "";
    await saveSettings(settingsPayload("set"), false);
    toast("Folder deleted");
  }

  async function pollSystemStats() {
    const { ok, data } = await apiGet("/api/system", { fallback: {} });
    if (!ok || !data) return;
    const cpuEl = $("#stat-cpu .stat-value");
    const tempEl = $("#stat-temp .stat-value");
    const headerClock = $("#header-clock");
    if (!isAndroidApp() && data.cpu_percent != null && cpuEl) {
      cpuEl.textContent = `${data.cpu_percent}%`;
      cpuEl.className = "stat-value" + (data.cpu_percent > 80 ? " hot" : data.cpu_percent > 50 ? " warn" : "");
    }
    if (data.cpu_temp_c != null && tempEl) {
      tempEl.textContent = `${data.cpu_temp_c}°C`;
      tempEl.className = "stat-value" + (data.cpu_temp_c > 85 ? " hot" : data.cpu_temp_c > 70 ? " warn" : "");
    }
    if (state.settings.show_clock !== false && data.local_time && headerClock) {
      headerClock.textContent = data.local_time;
    }
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
    const { ok, data } = await apiGet("/api/network", { fallback: { nodes: [], edges: [] } });
    if (!ok) {
      toast("Could not load network map", "error");
      return;
    }
    networkGraphData = data;
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

    const verEl = $("#stat-version");
    if (data.version && verEl) verEl.textContent = `v${data.version}`;

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

    const identitiesEl = $("#identities");
    if (identitiesEl) {
      identitiesEl.innerHTML = idHtml || '<div class="empty-hint">No identities</div>';
    }

    const transportStatus = data.transports || {};
    state.transportStatus = transportStatus;

    const tcpBtn = $("#btn-announce-tcp");
    if (tcpBtn) {
      const hubOn = !!state.settings?.hub_enabled;
      const hubConnected = !!transportStatus.hub?.connected;
      const tcpActive = !!transportStatus.tcp?.active;
      tcpBtn.disabled = hubOn ? !hubConnected : !tcpActive;
      tcpBtn.title = hubOn
        ? (hubConnected
          ? "Announce on hub — discover other hub users"
          : "Hub not connected — check Settings → Network")
        : (tcpActive
          ? "Announce on TCP/LAN"
          : "TCP transport unavailable — restart the node");
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
      const meName = $("#me-name");
      if (meName) meName.textContent = primary.name;
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
    if (!el) return;
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
        if (p.transport === "serial" && lq != null) metrics.push(`${lq}% link`);
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
    if (!hashId) return;
    state.selectedPeer = hashId;
    state.selectedName = name;
    clearUnread(hashId);

    $("#chat-empty")?.classList.add("hidden");
    $("#chat-active")?.classList.remove("hidden");

    const peerNameEl = $("#chat-peer-name");
    if (peerNameEl) peerNameEl.textContent = name;
    setAvatar($("#peer-avatar"), name, hashId);

    refreshPeerStatus(hashId);

    const msgInput = $("#msg-input");
    const sendBtn = $("#send-btn");
    if (msgInput) msgInput.disabled = false;
    if (sendBtn) sendBtn.disabled = false;
    if (isAndroidApp()) {
      setAndroidView("chat");
    } else {
      msgInput?.focus();
    }

    renderContacts();
    loadMessages();
    if (!isPeerLinked(hashId) && !inTransferCooldown(hashId)) connectPeer(hashId, false);
    saveMobileSession();
  }

  function updatePeerStatus(text) {
    const meta = $("#chat-peer-meta");
    if (meta) meta.textContent = text;
  }

  function renderFileBubble(m, out) {
    const meta = m.metadata || {};
    const tid = meta.transfer_id || "";
    const live = mergeTransferMeta(tid, meta);
    const mediaType = mediaMsgType(m);
    const size = live.size || 0;
    const offset = live.offset || 0;
    const pct = size ? Math.min(100, Math.round((offset / size) * 100)) : 0;
    const speed = live.speed_mbps;
    const stateLabel = live.state || "transferring";
    const filename = live.filename || m.text || "file";
    const speedStr = speed ? ` · ${Number(speed).toFixed(2)} MB/s` : "";
    const fileUrl = tid ? `/api/transfers/${encodeURIComponent(tid)}/file` : "";
    const downloadUrl = fileUrl ? `${fileUrl}?download=1` : "";
    const cancelled = stateLabel === "cancelled";
    const failed = stateLabel === "failed";
    const stateClass = cancelled ? " cancelled" : failed ? " failed" : "";
    const canPreviewImage = mediaType === "image" && fileUrl && stateLabel === "complete";
    const canPreviewVideo = mediaType === "video" && fileUrl && stateLabel === "complete";
    const previewUrl = canPreviewImage || canPreviewVideo
      ? `${fileUrl}?v=${encodeURIComponent(stateLabel)}`
      : fileUrl;
    const progressLine = stateLabel === "complete"
      ? `${formatBytes(size)} · complete`
      : stateLabel === "paused"
        ? `${formatBytes(offset)} / ${formatBytes(size)} · paused`
        : `${formatBytes(offset)} / ${formatBytes(size)} · ${pct}%${speedStr}`;
    const progressBar = stateLabel !== "complete" && !cancelled && !failed
      ? `<div class="progress-track chat-progress"><div class="progress-fill" style="width:${pct}%"></div></div>`
      : "";
    const showDownload = stateLabel === "complete" && downloadUrl;
    const downloadLabel = meta.is_folder_zip ? "Download folder ZIP" : "Download file";
    const downloadLink = showDownload
      ? `<a class="file-download" href="${downloadUrl}" download="${escapeHtml(filename)}" target="_blank" rel="noopener">${downloadLabel}</a>`
      : "";

    if (canPreviewImage) {
      return `<div class="file-bubble image-bubble${stateClass}" data-transfer="${escapeHtml(tid)}">
        <button type="button" class="media-preview-btn" data-media-open="${escapeHtml(previewUrl)}" data-media-kind="image" data-media-name="${escapeHtml(filename)}">
          <img src="${previewUrl}" alt="${escapeHtml(filename)}" class="chat-image" loading="lazy" />
        </button>
        <div class="file-name">${escapeHtml(filename)}</div>
        <div class="file-progress-meta">${cancelled ? "Transfer cancelled" : progressLine}</div>
        ${progressBar}
        ${downloadLink}
      </div>`;
    }

    if (canPreviewVideo) {
      return `<div class="file-bubble video-bubble${stateClass}" data-transfer="${escapeHtml(tid)}">
        <button type="button" class="media-preview-btn" data-media-open="${escapeHtml(previewUrl)}" data-media-kind="video" data-media-name="${escapeHtml(filename)}">
          <video src="${previewUrl}" class="chat-video" controls preload="metadata" playsinline></video>
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
    const { ok, data } = await apiDelete(`/api/messages/${encodeURIComponent(messageId)}`);
    if (ok && data?.deleted) {
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
    state.transfers[data.id] = { ...state.transfers[data.id], ...data };
    const idx = state.messageCache.findIndex((m) => m.metadata?.transfer_id === data.id);
    if (idx < 0) return;
    const meta = state.messageCache[idx].metadata || {};
    const metaState = meta.state;
    const dataState = data.state;
    const mergedState = dataState ?? metaState;
    const bestState = (TERMINAL_TRANSFER_STATES.has(metaState) && !TERMINAL_TRANSFER_STATES.has(dataState))
      ? metaState
      : mergedState;
    state.messageCache[idx].metadata = {
      ...meta,
      transfer_id: data.id,
      state: bestState,
      offset: data.offset ?? meta.offset,
      size: data.size ?? meta.size,
      speed_mbps: data.speed_mbps ?? meta.speed_mbps,
      filename: data.filename ?? meta.filename,
      is_folder_zip: data.metadata?.is_folder_zip ?? meta.is_folder_zip,
      folder_name: data.metadata?.folder_name ?? meta.folder_name,
    };
  }

  function bubbleNeedsMediaRender(msg, bubble, data) {
    if (!msg) return false;
    const mediaType = mediaMsgType(msg);
    if (mediaType !== "image" && mediaType !== "video") return false;
    const tid = msg.metadata?.transfer_id || data?.id || "";
    const merged = mergeTransferMeta(tid, msg.metadata || {});
    const stateLabel = data?.state || merged.state || "";
    if (stateLabel !== "complete") return false;
    if (!bubble) return true;
    return !bubble.classList.contains("image-bubble") && !bubble.classList.contains("video-bubble");
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
    if (scroll && msg && (mediaMsgType(msg) === "image" || mediaMsgType(msg) === "video") && data.state === "complete") {
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
    const paused = stateLabel === "paused";
    const meta = bubble.querySelector(".file-progress-meta");
    if (meta) {
      meta.textContent = cancelled
        ? "Transfer cancelled"
        : failed
          ? "Transfer failed"
          : stateLabel === "complete"
            ? `${formatBytes(size)} · complete`
            : paused
              ? `${formatBytes(offset)} / ${formatBytes(size)} · paused`
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
    }, 200);
  }

  function renderMessages(msgs, { preserveScroll = false, scrollToBottom = false } = {}) {
    const el = $("#messages");
    if (!el) return;
    const wasAtBottom = scrollToBottom || (preserveScroll ? isNearBottom(el) : true);
    state.messageCache = msgs;
    let lastDate = "";
    let html = "";

    msgs.forEach((m) => {
      try {
      const date = formatDate(m.timestamp);
      if (date !== lastDate) {
        html += `<div class="date-sep">${date}</div>`;
        lastDate = date;
      }
      const out = isOutgoing(m.sender_hash);
      const mediaType = mediaMsgType(m);
      const isFile = mediaType === "file" || mediaType === "image" || mediaType === "video";
      const body = isFile ? renderFileBubble(m, out) : escapeHtml(m.text);
      const actions = isFile
        ? ""
        : `<div class="bubble-actions">
            <button type="button" class="bubble-action icon-only" data-copy="${escapeHtml(m.id)}" title="Copy" aria-label="Copy">${ICON_COPY}</button>
            <button type="button" class="bubble-action icon-only danger" data-del-msg="${escapeHtml(m.id)}" title="Delete" aria-label="Delete">${ICON_TRASH}</button>
          </div>`;
      html += `<div class="bubble-row ${out ? "out" : "in"}" data-msg-id="${escapeHtml(m.id)}">
        <div class="bubble ${out ? "out" : "in"} ${mediaType === "image" ? "image" : ""}">
          ${actions}
          ${body}
          <div class="bubble-meta">
            <span>${formatTime(m.timestamp)}</span>
            <span class="bubble-status ${m.status}"></span>
          </div>
        </div>
      </div>`;
      } catch (_) { /* skip malformed message */ }
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
        ev.preventDefault();
        ev.stopPropagation();
        blurComposer();
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
    body.style.cursor = "grab";

    const startDrag = (clientX, clientY) => {
      if (!$("#media-lightbox")?.classList.contains("open")) return;
      if (state.mediaZoom <= 1) return;
      state.mediaDragging = true;
      state.mediaDragStart = {
        x: clientX - state.mediaPan.x,
        y: clientY - state.mediaPan.y,
      };
      body.style.cursor = "grabbing";
    };

    const moveDrag = (clientX, clientY) => {
      if (!state.mediaDragging || !state.mediaDragStart) return;
      state.mediaPan = {
        x: clientX - state.mediaDragStart.x,
        y: clientY - state.mediaDragStart.y,
      };
      applyMediaZoom();
    };

    const endDrag = () => {
      state.mediaDragging = false;
      state.mediaDragStart = null;
      body.style.cursor = "grab";
    };

    body.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      startDrag(e.clientX, e.clientY);
    });
    window.addEventListener("mousemove", (e) => moveDrag(e.clientX, e.clientY));
    window.addEventListener("mouseup", endDrag);

    body.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1) return;
      startDrag(e.touches[0].clientX, e.touches[0].clientY);
    }, { passive: true });
    body.addEventListener("touchmove", (e) => {
      if (!state.mediaDragging || e.touches.length !== 1) return;
      e.preventDefault();
      moveDrag(e.touches[0].clientX, e.touches[0].clientY);
    }, { passive: false });
    body.addEventListener("touchend", endDrag);
    body.addEventListener("touchcancel", endDrag);
  }

  function openMediaLightbox(url, kind, filename) {
    blurComposer();
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
    const video = body?.querySelector("video");
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    if (body) body.innerHTML = "";
    state.mediaZoom = 1;
    state.mediaPan = { x: 0, y: 0 };
    state.mediaDragging = false;
  }

  function updateChatTransfer(data) {
    if (!data.id) return;
    state.transfers[data.id] = { ...state.transfers[data.id], ...data };
    const peer = state.selectedPeer;
    if (!peer) return;
    const peerNorm = normalizeHash(peer);
    const relevant = normalizeHash(data.sender_hash) === peerNorm
      || normalizeHash(data.recipient_hash) === peerNorm;
    if (!relevant) return;
    const idx = state.messageCache.findIndex((m) => m.metadata?.transfer_id === data.id);
    if (idx >= 0) {
      const meta = state.messageCache[idx].metadata || {};
      const merged = mergeTransferMeta(data.id, meta);
      state.messageCache[idx].metadata = {
        ...meta,
        transfer_id: data.id || meta.transfer_id,
        state: merged.state,
        offset: merged.offset,
        size: merged.size,
        speed_mbps: merged.speed_mbps,
        filename: merged.filename,
      };
      if (TERMINAL_TRANSFER_STATES.has(merged.state)) {
        refreshTransferBubble(data.id, mergeTransferMeta(data.id, state.messageCache[idx].metadata), {
          scroll: merged.state === "complete",
        });
        return;
      }
      scheduleTransferPatch(data.id, data);
    } else {
      loadMessages();
    }
  }

  function upsertChatMessage(m) {
    const idx = state.messageCache.findIndex((x) => x.id === m.id);
    if (idx >= 0) {
      state.messageCache[idx] = m;
    } else {
      state.messageCache.push(m);
      state.messageCache.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
    }
    syncTransferStateFromMessage(m);
  }

  function onNewMessage(m) {
    if (!messageForSelectedPeer(m)) {
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

    const msgMedia = mediaMsgType(m);
    const tid = m.metadata?.transfer_id;
    const prev = state.messageCache.find((x) => x.id === m.id);
    upsertChatMessage(m);

    if (msgMedia === "file" || msgMedia === "image" || msgMedia === "video") {
      const merged = tid ? mergeTransferMeta(tid, m.metadata || {}) : m.metadata || {};
      const live = tid ? state.transfers[tid] : null;
      const transferData = live || merged;
      if (tid && TERMINAL_TRANSFER_STATES.has(merged.state)
          && (msgMedia === "image" || msgMedia === "video")) {
        refreshTransferBubble(tid, transferData, { scroll: merged.state === "complete" });
        return;
      }
      if (tid && patchTransferBubble(tid, transferData)) {
        if (ACTIVE_TRANSFER_STATES.has(merged.state)) scheduleTransferPatch(tid, transferData);
        return;
      }
      if (prev
          && prev.metadata?.state === m.metadata?.state
          && prev.metadata?.offset === m.metadata?.offset
          && prev.text === m.text) {
        return;
      }
    }

    renderMessages(state.messageCache, {
      preserveScroll: isOutgoing(m.sender_hash),
      scrollToBottom: !isOutgoing(m.sender_hash),
    });
  }

  function activeTransfersFromState(extra = []) {
    const seen = new Set();
    const items = [];
    const add = (t) => {
      if (!t?.id || seen.has(t.id)) return;
      const st = t.state || "";
      if (DOCK_TERMINAL_STATES.has(st)) return;
      if (!ACTIVE_TRANSFER_STATES.has(st)) return;
      seen.add(t.id);
      items.push(t);
    };
    Object.values(state.transfers).forEach(add);
    extra.forEach(add);
    return items;
  }

  function transferDockHtml(t) {
    const pct = t.size ? Math.min(100, Math.round((t.offset / t.size) * 100)) : 0;
    const speed = t.speed_mbps ? ` · ${Number(t.speed_mbps).toFixed(2)} MB/s` : "";
    const terminal = DOCK_TERMINAL_STATES.has(t.state);
    const canCancel = ACTIVE_TRANSFER_STATES.has(t.state);
    return `<div class="transfer-dock-item" data-transfer-id="${escapeHtml(t.id)}">
      <div class="transfer-dock-info">
        <div class="transfer-dock-name">${escapeHtml(t.filename || "file")}</div>
        <div class="transfer-dock-meta">${escapeHtml(t.state || "transferring")} · ${pct}%${speed}</div>
        ${terminal ? "" : `<div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>`}
      </div>
      <button type="button" class="transfer-dock-cancel" data-cancel-transfer="${escapeHtml(t.id)}"
        title="Cancel transfer" aria-label="Cancel transfer" ${canCancel ? "" : "disabled"}>✕</button>
    </div>`;
  }

  async function cancelTransfer(transferId) {
    if (!transferId) return;
    const { ok, data } = await safeFetch(
      `/api/transfers/${encodeURIComponent(transferId)}/cancel`,
      { method: "POST" },
      { fallback: {} }
    );
    if (!ok) {
      toast(data?.error || "Could not cancel transfer", "error");
      return;
    }
    if (state.transfers[transferId]) {
      state.transfers[transferId] = { ...state.transfers[transferId], state: "cancelled" };
    }
    pruneTerminalTransfers();
    renderTransferDock();
    renderTransfers();
    if (state.selectedPeer) loadMessages();
  }

  function pruneTerminalTransfers() {
    Object.keys(state.transfers).forEach((id) => {
      const st = state.transfers[id]?.state;
      if (st && DOCK_TERMINAL_STATES.has(st)) {
        delete state.transfers[id];
      }
    });
  }

  function renderTransferDock(apiTransfers = null) {
    const dock = $("#transfer-dock");
    const list = $("#transfer-dock-list");
    if (!dock || !list) return;
    pruneTerminalTransfers();
    const active = activeTransfersFromState(apiTransfers || []);
    if (!active.length) {
      dock.classList.add("hidden");
      list.innerHTML = "";
      return;
    }
    dock.classList.remove("hidden");
    list.innerHTML = active.map(transferDockHtml).join("");
  }

  async function renderTransfers() {
    const el = $("#transfers");
    if (!el) return;
    if (state.loadingTransfers) return;
    state.loadingTransfers = true;
    try {
      const { ok, data } = await safeFetch(
        "/api/transfers",
        {},
        { silent: true, fallback: [] }
      );
      const all = ok ? (data || []) : activeTransfersFromState();
      const transfers = all.filter((t) => ACTIVE_TRANSFER_STATES.has(t.state));
      renderTransferDock(all);
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
            <div class="transfer-state">${escapeHtml(t.state)} · ${pct}%${speed}</div>
            <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
          </div>`;
        })
        .join("");
    } finally {
      state.loadingTransfers = false;
    }
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

  const MOBILE_SESSION_KEY = "srltcp_mobile_session";

  function isMobileLayout() {
    return document.documentElement.classList.contains("mobile-layout");
  }

  function isAndroidApp() {
    return document.documentElement.classList.contains("android-app");
  }

  function blurComposer() {
    const input = $("#msg-input");
    if (input && typeof input.blur === "function") input.blur();
    const active = document.activeElement;
    if (active && active !== document.body && typeof active.blur === "function") {
      active.blur();
    }
  }

  function mobileSessionStorage() {
    return isAndroidApp() ? localStorage : sessionStorage;
  }

  function setAndroidView(view, { skipHistory = false } = {}) {
    if (!isAndroidApp()) return;
    const prev = state.androidView;
    state.androidView = view;
    const sidebar = $("#sidebar");
    const chat = $("#chat-panel");
    if (view === "sidebar") {
      sidebar?.classList.remove("android-nav-hidden");
      chat?.classList.remove("android-nav-visible");
    } else if (view === "chat") {
      sidebar?.classList.add("android-nav-hidden");
      chat?.classList.add("android-nav-visible");
    }
    if (!skipHistory && prev !== view && view !== "settings") {
      try {
        history.pushState({ srltcpAndroid: view }, "");
      } catch (_) { /* ignore */ }
    }
    saveMobileSession();
  }

  function androidNavigateBack() {
    if (!isAndroidApp()) return false;
    if ($("#media-lightbox")?.classList.contains("open")) {
      closeMediaLightbox();
      return true;
    }
    if ($("#settings-window") && !$("#settings-window").classList.contains("hidden")) {
      closeSettings();
      return true;
    }
    if (state.androidView === "chat") {
      setAndroidView("sidebar", { skipHistory: true });
      return true;
    }
    return false;
  }

  window.androidNavigateBack = androidNavigateBack;

  function saveMobileSession() {
    if (!isAndroidApp()) return;
    try {
      mobileSessionStorage().setItem(
        MOBILE_SESSION_KEY,
        JSON.stringify({
          selectedPeer: state.selectedPeer,
          selectedName: state.selectedName,
          androidView: state.androidView,
        })
      );
    } catch (_) { /* ignore */ }
  }

  function peekMobileSession() {
    if (!isAndroidApp()) return null;
    try {
      const raw = mobileSessionStorage().getItem(MOBILE_SESSION_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }

  function openSettings() {
    const panel = $("#settings-window");
    if (!panel) return;
    if (isAndroidApp()) {
      blurComposer();
      try {
        history.pushState({ srltcpAndroid: "settings" }, "");
      } catch (_) { /* ignore */ }
    }
    panel.classList.remove("hidden");
    panel.setAttribute("aria-hidden", "false");
    state.settingsFormDirty = true;
    const s = state.settings || {};
    fillSettingsForm(s);
    loadSerialSettings(s.serial_port || "", s.serial_baud || 57600);
  }

  function closeSettings() {
    const panel = $("#settings-window");
    if (!panel) return;
    panel.classList.add("hidden");
    panel.setAttribute("aria-hidden", "true");
    state.settingsFormDirty = false;
    if (isAndroidApp()) {
      setAndroidView("sidebar", { skipHistory: true });
    }
  }

  function applyRestoredPeer() {
    const saved = state._restorePeer;
    if (!saved?.selectedPeer) return;
    delete state._restorePeer;
    const peer = state.peers.find((p) => p.hash_id === saved.selectedPeer)
      || state.trusted.find((p) => p.hash_id === saved.selectedPeer);
    const name = saved.selectedName || peer?.name || saved.selectedPeer.slice(0, 8);
    selectPeer(saved.selectedPeer, name);
    if (isAndroidApp()) {
      setAndroidView("chat", { skipHistory: true });
    } else {
      closeSidebarMobile();
    }
  }

  function openSidebarMobile() {
    if (!isMobileLayout()) return;
    $("#sidebar")?.classList.add("open");
    const backdrop = $("#sidebar-backdrop");
    backdrop?.classList.remove("hidden");
    backdrop?.setAttribute("aria-hidden", "false");
  }

  function closeSidebarMobile() {
    $("#sidebar")?.classList.remove("open");
    const backdrop = $("#sidebar-backdrop");
    backdrop?.classList.add("hidden");
    backdrop?.setAttribute("aria-hidden", "true");
  }

  function toggleSidebarMobile() {
    if ($("#sidebar")?.classList.contains("open")) closeSidebarMobile();
    else openSidebarMobile();
  }

  function applyMobileLayout(platform) {
    if (platform == null && !document.documentElement.classList.contains("android-app")) {
      if (!window.matchMedia("(max-width: 768px)").matches) {
        document.documentElement.classList.remove("mobile-layout");
        closeSidebarMobile();
        return;
      }
    }
    const root = document.documentElement;
    const narrow = window.matchMedia("(max-width: 768px)").matches;
    const android =
      platform === "android" || root.classList.contains("android-app");
    if (!android && !narrow) return;
    root.classList.add("mobile-layout");
    if (android) root.classList.add("android-app");
    if (android) {
      const saved = peekMobileSession();
      if (saved?.selectedPeer && saved.androidView === "chat") {
        state._restorePeer = saved;
      } else {
        setAndroidView("sidebar", { skipHistory: true });
      }
    }
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

  function openSettingsFromMobile() {
    if (isAndroidApp()) {
      openSettings();
      return;
    }
    closeSidebarMobile();
    openSettings();
  }

  $("#btn-settings")?.addEventListener("click", openSettingsFromMobile);
  $("#btn-settings-top")?.addEventListener("click", openSettingsFromMobile);
  $("#btn-settings-chat")?.addEventListener("click", openSettingsFromMobile);
  $("#btn-close-settings")?.addEventListener("click", closeSettings);
  $("#settings-window-overlay")?.addEventListener("click", closeSettings);
  $("#btn-menu")?.addEventListener("click", toggleSidebarMobile);
  $("#sidebar-backdrop")?.addEventListener("click", closeSidebarMobile);
  document.querySelectorAll(".settings-tab").forEach((tab) => {
    tab.addEventListener("click", () => switchSettingsTab(tab.dataset.tab));
  });
  $("#set-clock-source")?.addEventListener("change", toggleNtpField);
  $("#btn-back")?.addEventListener("click", () => {
    if (isAndroidApp()) {
      setAndroidView("sidebar");
      saveMobileSession();
      return;
    }
    $("#chat-active")?.classList.add("hidden");
    $("#chat-empty")?.classList.remove("hidden");
    openSidebarMobile();
    state.selectedPeer = null;
    state.selectedName = null;
    renderContacts();
    saveMobileSession();
  });

  $("#peer-search")?.addEventListener("input", debounce((e) => {
    state.search = e.target.value;
    renderContacts();
  }, 200));

  $("#send-btn")?.addEventListener("mousedown", (e) => {
    if (isAndroidApp()) e.preventDefault();
  });
  $("#send-btn")?.addEventListener("touchstart", (e) => {
    if (isAndroidApp()) e.preventDefault();
  }, { passive: false });
  $("#send-btn")?.addEventListener("click", (e) => {
    if (isAndroidApp()) e.preventDefault();
    sendMessage();
  });

  $("#msg-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  $("#msg-input")?.addEventListener("input", (e) => autoResize(e.target));

  $("#btn-send-folder-peer")?.addEventListener("click", () => {
    if (!state.selectedPeer) {
      toast("Select a peer first");
      return;
    }
    openFolderSendPicker(state.selectedPeer, state.selectedName);
  });

  $("#btn-file")?.addEventListener("click", () => $("#file-input")?.click());

  $("#file-input")?.addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (file) sendFile(file);
    e.target.value = "";
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if ($("#media-lightbox")?.classList.contains("open")) {
        closeMediaLightbox();
        return;
      }
      closeSettings();
    }
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
      display_name: ($(`#${prefix}-name`)?.value || "").trim(),
      web_port: parseInt($(`#${prefix}-web-port`)?.value || "9876", 10),
      tcp_port: parseInt($("#set-tcp-port")?.value || "7825", 10),
      discovery_port: parseInt($("#set-discovery-port")?.value || "7826", 10),
      strict_ports: $("#set-strict-ports")?.checked !== false,
      message_retention_preset: $(`#${prefix}-retention`)?.value || "1w",
      incoming_files_dir: $(`#${prefix}-incoming`)?.value.trim() || "",
      shared_folder: $(`#${prefix}-shared`)?.value.trim() || "",
      lan_ip: $(`#${prefix}-lan-ip`)?.value || "",
      auto_announce: $(`#${prefix}-auto-announce`)?.checked || false,
      hub_enabled: $("#set-hub-enabled")?.checked || false,
      hub_host: ($("#set-hub-host")?.value || "").trim(),
      hub_lan_host: ($("#set-hub-lan-host")?.value || "").trim(),
      hub_port: parseInt($("#set-hub-port")?.value || "7825", 10),
      wan_expose_port: $("#set-wan-expose")?.checked || false,
      handshake_protocol: $("#set-handshake-protocol")?.value || "identity",
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
    const { ok, data } = await apiGet(url, { fallback: {} });
    if (!ok || !data?.path) {
      toast("Could not browse folder", "error");
      return;
    }
    const crumb = $("#folder-crumb");
    const list = $("#folder-list");
    if (!crumb || !list) return;
    crumb.textContent = data.path;
    let html = "";
    if (data.parent && data.parent !== data.path) {
      html += `<button type="button" class="folder-entry" data-path="${escapeHtml(data.parent)}">..</button>`;
    }
    html += (data.entries || []).filter((e) => e.type === "dir").map((e) =>
      `<button type="button" class="folder-entry" data-path="${escapeHtml(e.path)}">${escapeHtml(e.name)}</button>`
    ).join("");
    list.innerHTML = html || '<div class="empty-hint">No folders</div>';
    list.querySelectorAll(".folder-entry").forEach((btn) => {
      btn.addEventListener("click", () => browseFolder(btn.dataset.path));
    });
  }

  $("#transfer-dock")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-cancel-transfer]");
    if (!btn || btn.disabled) return;
    e.stopPropagation();
    cancelTransfer(btn.dataset.cancelTransfer);
  });

  $("#settings-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    await saveSettings(settingsPayload("set"), false);
  });

  $("#setup-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const port = parseInt($("#setup-web-port")?.value || "9876", 10);
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
    const { ok, data } = await apiGet("/api/release-notes", { fallback: { notes: "" } });
    if (!ok) {
      toast("Could not load release notes", "error");
      return;
    }
    $("#release-notes-body").textContent = data?.notes || "";
    $("#release-modal").classList.add("open");
  });

  $("#btn-restart")?.addEventListener("click", async () => {
    if (!confirm("Restart SRLTCP?")) return;
    await apiPost("/api/restart", {}, { silent: true });
    toast("Restarting…");
    setTimeout(() => location.reload(), 3000);
  });

  document.addEventListener("click", (e) => {
    const deleteFolderBtn = e.target.closest("[data-folder-delete]");
    if (deleteFolderBtn) {
      deleteSettingsFolder(deleteFolderBtn.dataset.folderDelete);
      return;
    }

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
          const { ok } = await apiPatch(`/api/trusted/${encodeURIComponent(hashId)}`, {
            blocked: false,
          });
          if (!ok) {
            toast("Unblock failed", "error");
            return;
          }
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
      closeModal($("#folder-modal"));
      await sendFolderToPeer(path, hashId, peerName);
      return;
    }
    if (state.folderTarget) $(`#${state.folderTarget}`).value = path;
    closeModal($("#folder-modal"));
  });

  $("#folder-cancel")?.addEventListener("click", () => closeModal($("#folder-modal")));
  $("#release-close")?.addEventListener("click", () => closeModal($("#release-modal")));
  $("#btn-share-folder")?.addEventListener("click", () => openShareModal("offer"));
  $("#share-close")?.addEventListener("click", closeShareModal);
  setupModalClose($("#share-modal"), closeShareModal);
  $("#wan-save")?.addEventListener("click", saveWanSettings);
  $("#wan-cancel")?.addEventListener("click", closeWanModal);
  setupModalClose($("#wan-modal"), () => { state.wanModalTarget = null; });
  $("#media-lightbox-close")?.addEventListener("click", closeMediaLightbox);
  setupModalClose($("#media-lightbox"), closeMediaLightbox);
  $("#media-zoom-in")?.addEventListener("click", () => setMediaZoom(0.25));
  $("#media-zoom-out")?.addEventListener("click", () => setMediaZoom(-0.25));
  $("#media-zoom-reset")?.addEventListener("click", resetMediaZoom);
  $("#media-lightbox-body")?.addEventListener("wheel", (e) => {
    if (!$("#media-lightbox")?.classList.contains("open")) return;
    e.preventDefault();
    setMediaZoom(e.deltaY < 0 ? 0.15 : -0.15);
  }, { passive: false });

  loadUnreadState();

  function startInterval(fn, ms) {
    const id = setInterval(fn, ms);
    state.timers.push(id);
    return id;
  }

  window.addEventListener("beforeunload", () => {
    state.pageUnloading = true;
    state.timers.forEach(clearInterval);
    state.timers.length = 0;
    if (state.wsReconnectTimer) clearTimeout(state.wsReconnectTimer);
    if (state.transferPatchTimer) clearTimeout(state.transferPatchTimer);
    if (state.ws) {
      state.ws.onclose = null;
      state.ws.onerror = null;
      try { state.ws.close(); } catch (_) { /* ignore */ }
      state.ws = null;
    }
  });

  window.addEventListener("unhandledrejection", (ev) => {
    console.warn("Unhandled promise rejection:", ev.reason);
    if (!state.pageUnloading) {
      toast("Something went wrong — check the console", "error");
    }
    ev.preventDefault();
  });

  $("#btn-network-viz")?.addEventListener("click", async () => {
    openModal($("#network-modal"));
    await renderNetworkGraph();
  });
  $("#network-close")?.addEventListener("click", () => {
    closeModal($("#network-modal"));
    stopNetworkAnimation();
  });
  $("#network-refresh")?.addEventListener("click", () => renderNetworkGraph());

  $("#identities")?.addEventListener("click", async (e) => {
    const regen = e.target.closest("[data-regen]");
    const del = e.target.closest("[data-del-id]");
    if (regen && confirm(`Regenerate ${regen.dataset.regen} identity?`)) {
      const { ok } = await safeFetch(
        `/api/identities/${regen.dataset.regen}/regenerate`,
        { method: "POST" },
        { fallback: {} }
      );
      if (!ok) toast("Identity regenerate failed", "error");
      else await fetchStatus();
    }
    if (del && confirm(`Delete ${del.dataset.delId} identity?`)) {
      const { ok } = await apiDelete(`/api/identities/${del.dataset.delId}`);
      if (!ok) toast("Identity delete failed", "error");
      else await fetchStatus();
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
  startInterval(pollSystemStats, 10000);
  startInterval(async () => {
    if (state.settings.show_clock === false) return;
    const { ok, data } = await safeFetch("/api/system", {}, { silent: true, fallback: {} });
    if (ok && data.local_time && $("#header-clock")) {
      $("#header-clock").textContent = data.local_time;
    }
  }, 1000);
  startInterval(() => { loadPeers().catch(() => {}); }, 5000);
  startInterval(renderTransferDock, 3000);
  startInterval(() => {
    if (!state.wsConnected && state.selectedPeer && $("#chat-active") && !$("#chat-active").classList.contains("hidden")) {
      loadMessages().catch(() => {});
    }
  }, 4000);


  async function initApp() {
    applyMobileLayout(
      document.documentElement.classList.contains("android-app")
        ? "android"
        : null
    );
    const [settingsRes, versionRes] = await Promise.all([
      apiGet("/api/settings", { fallback: {} }),
      apiGet("/api/version", { fallback: {} }),
    ]);
    if (versionRes.ok && versionRes.data?.platform) {
      state.settings = state.settings || {};
      state.settings.platform = versionRes.data.platform;
      applyMobileLayout(versionRes.data.platform);
      if (versionRes.data.platform === "android") {
        $("#stat-cpu")?.classList.add("hidden");
      }
    }
    if (settingsRes.ok && settingsRes.data) {
      state.settings = settingsRes.data;
      showSetupIfNeeded(settingsRes.data);
      fillSettingsForm(settingsRes.data);
      applyClockVisibility();
    }
    if (versionRes.ok && versionRes.data?.version) {
      const ver = $("#stat-version");
      if (ver) ver.textContent = `v${versionRes.data.version}`;
    }
    const status = await fetchStatus();
    if (!status || !Object.keys(status).length) {
      toast("Failed to load status", "error");
    }
  }

  window.applyMobileLayout = applyMobileLayout;

  window.addEventListener("popstate", () => {
    if (isAndroidApp()) androidNavigateBack();
  });

  document.addEventListener("visibilitychange", () => {
    saveMobileSession();
    if (!document.hidden && document.documentElement.classList.contains("android-app")) {
      try {
        if (window.Notification && Notification.permission === "default") {
          Notification.requestPermission();
        }
      } catch (_) { /* ignore */ }
    }
  });

  window.addEventListener("resize", () => {
    applyMobileLayout(
      document.documentElement.classList.contains("android-app")
        ? "android"
        : null
    );
  });

  initApp().catch((err) => {
    console.warn("init failed:", err);
    toast("Failed to initialize app", "error");
  });
})();