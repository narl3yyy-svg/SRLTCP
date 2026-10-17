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
    groups: [],
    selectedGroup: null,
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
    transferReloads: new Set(),
    dropTargetHash: null,
    folderSendTarget: null,
    wsConnected: false,
    wsReconnectTimer: null,
    timers: [],
    pageUnloading: false,
    loadingPeers: false,
    androidView: "sidebar",

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

  function mediaMsgType(msg) {
    const t = msg?.msg_type;
    if (t === "image" || t === "video") return t;
    if (t === "text" || t === "group_text") return "text";  // "see photo.png" is still text
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
    const gid = m.metadata?.group_id;
    if (state.selectedGroup) return gid === state.selectedGroup;
    if (gid) return false;  // group messages never show in a one-to-one chat
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
      // Catch up on anything that arrived while the socket was down.
      if (state.selectedPeer) loadMessages().catch(() => {});
      loadPeers().catch(() => {});
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
              existing.address = data.address || "";
            } else {
              state._lastLinks.push({
                hash_id: data.hash_id,
                handshake_complete: true,
                transport: data.transport,
                address: data.address || "",
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
          logActivity(`Link up: ${data.name || data.hash_id?.slice(0, 8)}`);
          break;
        case "link_down":
          if (inTransferCooldown(data.hash_id)) {
            if (state.selectedPeer === data.hash_id) refreshPeerStatus(data.hash_id);
            break;
          }
          delete state.links[data.hash_id];
          delete state.linkMetrics[data.hash_id];
          // The cached link list is only refreshed by the next poll; drop the
          // dead link now so the header and file sends don't think it's up.
          state._lastLinks = (state._lastLinks || []).filter((l) => l.hash_id !== data.hash_id);
          loadPeers();
          if (state.selectedPeer === data.hash_id) {
            refreshPeerStatus(data.hash_id);
          }
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
          // Progress lives in the file's own bubble, in the chat with the
          // contact it is going to or coming from — nowhere else.
          syncTransferMessage(data);
          if (state.selectedPeer) updateChatTransfer(data);
          if (type === "transfer_complete") {
            markTransferCooldown(data.sender_hash, data.recipient_hash);
            notifyUser(
              "Transfer complete",
              data.filename || "File received",
              { tag: `transfer-${data.id}` }
            );
          }
          if (TERMINAL_TRANSFER_STATES.has(data.state)) setTimeout(pruneTerminalTransfers, 600);
          break;
        case "share_offer": {
          const from = peerByHash(data.hash_id)?.name || data.hash_id?.slice(0, 8);
          loadShareGrants()
            .then(() => renderShareGrants())
            .catch(() => {});
          toast(`${from} shared “${data.label || "files"}” with you — open Share → Shared with me`);
          notifyUser("Shared with you", `${from} shared “${data.label || "files"}”`, { tag: `share-${data.grant_id}` });
          break;
        }
        case "share_listing":
          break;  // the listing request itself returns the result
        case "group_update":
          onGroupUpdate(data.group);
          break;
        case "messages_removed":
          onMessagesRemoved(data.ids);
          break;
        case "sync_invite":
          onSyncInvite(data.sync);
          break;
        case "sync_update":
          if (state.syncPeer && data.sync?.peer === state.syncPeer) loadSyncs();
          break;
        case "contact_request":
          loadPeers();
          showContactRequest({ ...data, _event: true });
          notifyUser(
            "Contact request",
            `${data.name || data.hash_id?.slice(0, 8)} wants to add you as a contact`,
            { tag: `req-${data.hash_id}` }
          );
          logActivity(`Contact request: ${data.name || data.hash_id?.slice(0, 8)}`);
          break;
        case "paired": {
          // Someone scanned our QR / opened our link: they're a contact now
          // on both sides. Show them, like on the scanning device.
          const label = data.name || data.hash_id?.slice(0, 8);
          toast(`${label} scanned your code — you're now contacts`);
          logActivity(`Paired with ${label}`);
          setPeerTab("trusted");
          loadPeers().then(() => {
            if ($("#pair-modal")?.classList.contains("open")) {
              closePairModal();
              if (data.hash_id) selectPeer(data.hash_id, label);
            }
          });
          break;
        }
        case "call_signal":
          if (typeof window.srltcpOnCallSignal === "function") window.srltcpOnCallSignal(data);
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

  function setPeerTab(tab) {
    state.peerTab = tab;
    document.querySelectorAll(".peer-tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  }

  /* ── API ── */
  async function loadPeers() {
    if (state.loadingPeers) return;
    state.loadingPeers = true;
    try {
      const [pRes, tRes, gRes] = await Promise.all([
        safeFetch("/api/peers", {}, { silent: true, fallback: [] }),
        safeFetch("/api/trusted", {}, { silent: true, fallback: [] }),
        safeFetch("/api/groups", {}, { silent: true, fallback: [] }),
      ]);
      if (pRes.ok) state.peers = pRes.data || [];
      if (gRes.ok) {
        state.groups = gRes.data || [];
        if (state.selectedGroup) updateGroupHeader();
      }
      if (tRes.ok) {
        state.trusted = tRes.data || [];
        dropAnsweredRequests();
      }
      if (tRes.ok && !state.peerTabChosen) {
        // First load: open on Trusted once there is someone to talk to.
        state.peerTabChosen = true;
        setPeerTab(state.trusted.some((t) => !t.blocked) ? "trusted" : "discovered");
      }
      renderContacts();
      if (state._restorePeer) applyRestoredPeer();
      state.peers.filter((p) => p.metadata?.contact_request).forEach((p) => showContactRequest(p));
    } finally {
      state.loadingPeers = false;
    }
  }

  /* Settings → Serial: what the radio link is doing (live). */
  function renderSerialLink(serial) {
    const el = $("#serial-link-status");
    if (!el) return;
    const link = serial?.link || {};
    if (!serial || !serial.active || !Object.keys(link).length) {
      el.textContent = "";
      el.className = "net-status";
      return;
    }
    const kb = (n) => (n >= 10240 ? `${Math.round(n / 1024)} KB` : `${n} B`);
    const ago = link.last_rx ? Math.max(0, Math.round(Date.now() / 1000 - link.last_rx)) : null;
    const peers = serial.peers || [];
    const lines = [
      peers.length
        ? `Connected over the radio: ${peers.join(", ")}`
        : link.packets_in
          ? "Hearing the other radio — connecting…"
          : "Port open — nothing heard from the other radio yet",
      `Sent ${kb(link.bytes_out || 0)} · received ${kb(link.bytes_in || 0)} · ${link.packets_in || 0} good / ${link.bad_packets || 0} bad packets · ${link.resent || 0} resent`
        + (ago !== null ? ` · last heard ${ago}s ago` : ""),
    ];
    if ((link.bytes_in || 0) > 200 && !link.packets_in) {
      lines.push("Data arrives but none of it is readable: check both sides use the same baud rate, and the radios are in transparent serial mode.");
    } else if ((link.bytes_out || 0) > 2000 && !(link.bytes_in || 0)) {
      lines.push("Nothing comes back: is the other phone's serial on, with the same baud rate? Are the radios paired (same channel / net ID / air speed)?");
    }
    el.textContent = lines.join("\n");
    el.className = `net-status ${peers.length ? "ok" : ""}`;
  }

  // Live radio stats while Settings → Serial is open.
  setInterval(() => {
    const panel = document.querySelector('.settings-panel[data-panel="serial"]');
    const modal = $("#settings-window");
    if (panel?.classList.contains("active") && modal && !modal.classList.contains("hidden")) {
      fetchStatus();
    }
  }, 3000);

  /* Settings → Network: Tor. */
  function renderTorStatus(tor) {
    const el = $("#tor-status");
    if (!el || !tor) return;
    let text = "";
    let ok = false;
    if (!tor.enabled) text = "Off.";
    else if (tor.state === "ready") {
      ok = true;
      text = "Tor: connected. Your contacts can reach you from any network.";
    } else if (tor.state === "starting") text = `Tor: connecting to the Tor network… ${tor.progress || 0}%`;
    else if (tor.state === "unavailable") text = `Tor: ${tor.detail || "not available"}`;
    else if (tor.state === "error") text = `Tor: problem — ${tor.detail || "retrying"}`;
    else text = "Tor: starting…";
    el.textContent = text;
    el.className = `net-status ${ok ? "ok" : ""}`;
  }

  /* ── Group chats ── */
  const GROUP_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>';

  function groupById(gid) {
    return (state.groups || []).find((g) => g.id === gid) || null;
  }

  function memberName(gid, hashId) {
    if (isOutgoing(hashId)) return "You";
    const contact = state.trusted.find((p) => p.hash_id === hashId);
    if (contact) return contact.name;
    const m = groupById(gid)?.members?.find((x) => x.hash_id === hashId);
    return m?.name || hashId.slice(0, 8);
  }

  function renderGroups() {
    const el = $("#contacts");
    const label = $("#contacts-label");
    if (!el) return;
    if (label) label.textContent = "Groups";
    const q = state.search.toLowerCase();
    const groups = (state.groups || []).filter((g) => !q || g.name.toLowerCase().includes(q));
    const newBtn = `<button type="button" class="action-btn group-new-btn" id="group-new">${GROUP_ICON} New group</button>`;
    if (!groups.length) {
      el.innerHTML = newBtn + `<div class="contacts-empty">No groups yet.<br>Create one with your trusted contacts.</div>`;
    } else {
      el.innerHTML = newBtn + groups.map((g) => {
        const active = state.selectedGroup === g.id ? " active" : "";
        const names = g.members.filter((m) => m.name !== "You").map((m) => m.name).join(", ");
        const unread = unreadCount(g.id);
        return `<div class="contact${active}" role="button" tabindex="0" data-group="${escapeHtml(g.id)}">
          <div class="avatar group-avatar" style="background:${hashColor(g.id)}22;color:${hashColor(g.id)};border-color:${hashColor(g.id)}44">${GROUP_ICON}</div>
          <div class="contact-info">
            <div class="contact-name">${escapeHtml(g.name)}${g.left ? '<span class="contact-request-tag">left</span>' : ""}</div>
            <div class="contact-preview"><span class="contact-endpoint">${g.members.length} members · ${escapeHtml(names)}</span></div>
          </div>
          <div class="contact-meta">${unread ? `<span class="unread-badge">${unread > 99 ? "99+" : unread}</span>` : ""}</div>
        </div>`;
      }).join("");
    }
    $("#group-new")?.addEventListener("click", () => openGroupModal(null));
    el.querySelectorAll("[data-group]").forEach((row) => {
      const open = () => { selectGroup(row.dataset.group); closeSidebarMobile(); };
      row.addEventListener("click", open);
      row.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
      });
      row.addEventListener("contextmenu", (ev) => { ev.preventDefault(); openGroupModal(row.dataset.group); });
    });
  }

  function updateGroupHeader() {
    const g = groupById(state.selectedGroup);
    if (!g) return;
    const nameEl = $("#chat-peer-name");
    if (nameEl) nameEl.textContent = g.name;
    const online = g.members.filter((m) => m.online).length;
    updatePeerStatus(g.left
      ? "You are no longer in this group"
      : `${g.members.length} members${online ? ` · ${online} online` : ""} · end-to-end encrypted`);
    const badge = $("#chat-peer-transport");
    badge?.classList.add("hidden");
    $("#chat-peer-verified")?.classList.add("hidden");
    const input = $("#msg-input");
    const sendBtn = $("#send-btn");
    if (input) {
      input.disabled = !!g.left;
      input.placeholder = g.left ? "You left this group" : "Message the group…";
    }
    if (sendBtn) sendBtn.disabled = !!g.left;
  }

  function selectGroup(gid) {
    const g = groupById(gid);
    if (!g) return;
    state.selectedPeer = gid;
    state.selectedGroup = gid;
    state.selectedName = g.name;
    state.stickBottom = true;
    state.selectedPeerShown = gid;
    clearUnread(gid);
    $("#chat-empty")?.classList.add("hidden");
    $("#chat-active")?.classList.remove("hidden");
    $("#chat-active")?.classList.add("group-chat");
    setAvatar($("#peer-avatar"), g.name, gid);
    updateGroupHeader();
    if (isAndroidApp()) setAndroidView("chat");
    else $("#msg-input")?.focus();
    renderContacts();
    state.messageCache = [];
    loadMessages();
    saveMobileSession();
  }

  function onGroupUpdate(group) {
    if (!group?.id) return;
    const known = groupById(group.id);
    state.groups = [...(state.groups || []).filter((g) => g.id !== group.id), group]
      .sort((a, b) => a.name.localeCompare(b.name));
    if (!known && !group.is_owner) {
      notifyUser(group.name, `You were added to the group “${group.name}”`, { tag: `group-${group.id}` });
    } else if (known && !known.left && group.left && !group.is_owner) {
      toast(`You are no longer in “${group.name}”`);
    }
    if (state.selectedGroup === group.id) updateGroupHeader();
    renderContacts();
  }

  function openGroupModal(gid) {
    const modal = $("#group-modal");
    if (!modal) return;
    const g = gid ? groupById(gid) : null;
    state.groupEditing = gid || null;
    const owner = !g || g.is_owner;
    $("#group-modal-title").textContent = !g ? "New group" : owner && !g.left ? "Edit group" : g.name;
    const nameInput = $("#group-name");
    nameInput.value = g ? g.name : "";
    nameInput.disabled = !owner || !!g?.left;
    const inGroup = new Set((g?.members || []).map((m) => m.hash_id));
    const list = $("#group-member-list");
    let rows;
    if (owner && !g?.left) {
      const contacts = state.trusted.filter((p) => !p.blocked);
      rows = contacts.map((p) => `<label class="group-member">
          <input type="checkbox" value="${escapeHtml(p.hash_id)}" ${inGroup.has(p.hash_id) ? "checked" : ""} />
          <span>${escapeHtml(p.name)}</span>
        </label>`).join("") || '<div class="contacts-empty">Trust someone first, then add them here.</div>';
    } else {
      rows = (g?.members || []).map((m) => `<div class="group-member">
          <span>${escapeHtml(m.name)}</span>
          <span class="member-note">${m.hash_id === g.owner ? "creator" : m.is_contact ? "contact" : ""}</span>
        </div>`).join("");
    }
    list.innerHTML = rows;
    $("#group-members-label").textContent = owner && !g?.left ? "Members" : `${g?.members?.length || 0} members`;
    $("#group-hint")?.classList.toggle("hidden", !(owner && !g?.left));
    const save = $("#group-save");
    save.textContent = !g ? "Create" : "Save";
    save.classList.toggle("hidden", !(owner && !g?.left));
    const leave = $("#group-leave");
    leave.classList.toggle("hidden", !g);
    leave.textContent = !g ? "" : g.left ? "Delete chat" : owner ? "Close group" : "Leave group";
    openModal(modal);
    if (!g) nameInput.focus();
  }

  async function saveGroupModal() {
    const gid = state.groupEditing;
    const name = $("#group-name").value.trim();
    const picked = [...$("#group-member-list").querySelectorAll("input[type=checkbox]:checked")].map((i) => i.value);
    if (!name) {
      toast("Give the group a name", "error");
      return;
    }
    let res;
    if (!gid) {
      if (!picked.length) {
        toast("Pick at least one contact", "error");
        return;
      }
      res = await apiPost("/api/groups", { name, members: picked });
    } else {
      const g = groupById(gid);
      const current = new Set(g.members.map((m) => m.hash_id));
      const add = picked.filter((h) => !current.has(h));
      const remove = g.members.map((m) => m.hash_id)
        .filter((h) => !picked.includes(h) && h !== g.owner);
      res = await apiPost(`/api/groups/${encodeURIComponent(gid)}`, { name, add, remove });
    }
    if (!res.ok) {
      toast(res.data?.error || "Could not save the group", "error");
      return;
    }
    closeModal($("#group-modal"));
    onGroupUpdate(res.data);
    setPeerTab("groups");
    renderContacts();
    selectGroup(res.data.id);
  }

  async function leaveGroupFromModal() {
    const g = groupById(state.groupEditing);
    if (!g) return;
    if (g.left) {
      if (!confirm(`Delete the chat “${g.name}” from this device?`)) return;
      const res = await safeFetch(`/api/groups/${encodeURIComponent(g.id)}`, { method: "DELETE" });
      if (!res.ok) return;
      state.groups = state.groups.filter((x) => x.id !== g.id);
      if (state.selectedGroup === g.id) {
        state.selectedGroup = null;
        state.selectedPeer = null;
        $("#chat-active")?.classList.add("hidden");
        $("#chat-active")?.classList.remove("group-chat");
        $("#chat-empty")?.classList.remove("hidden");
        if (isAndroidApp()) setAndroidView("sidebar");
      }
    } else {
      const what = g.is_owner ? "Close this group for everyone" : "Leave this group";
      if (!confirm(`${what}?`)) return;
      const res = await apiPost(`/api/groups/${encodeURIComponent(g.id)}/leave`, {});
      if (!res.ok) {
        toast(res.data?.error || "Could not leave", "error");
        return;
      }
      onGroupUpdate(res.data);
    }
    closeModal($("#group-modal"));
    renderContacts();
  }

  /* Incoming contact request: ask once, so pairing ends up mutual. */
  function showContactRequest(data) {
    const hashId = data?.hash_id;
    if (!hashId || state.trusted.some((t) => t.hash_id === hashId)) return;
    state.requestQueue = state.requestQueue || [];
    if (state.requestCurrent === hashId || state.requestQueue.some((r) => r.hash_id === hashId)) return;
    // Dismissed with Back: re-ask on the next real request event, not every poll.
    if (state.requestDismissed?.has(hashId) && !data._event) return;
    state.requestDismissed?.delete(hashId);
    state.requestQueue.push(data);
    if (!state.requestCurrent) nextContactRequest();
  }

  // A contact trusted another way (pairing, QR) while its prompt was open or
  // queued: the question is answered, so don't leave it covering the app.
  function dropAnsweredRequests() {
    const trusted = (h) => state.trusted.some((t) => t.hash_id === h);
    state.requestQueue = (state.requestQueue || []).filter((r) => !trusted(r.hash_id));
    if (state.requestCurrent && trusted(state.requestCurrent)) nextContactRequest();
  }

  function nextContactRequest() {
    const modal = $("#request-modal");
    const next = (state.requestQueue || []).shift();
    state.requestCurrent = next?.hash_id || null;
    if (!next || !modal) {
      closeModal(modal);
      return;
    }
    const name = next.name || next.hash_id.slice(0, 8);
    $("#request-text").textContent = `${name} added you as a contact. Add them back?`;
    $("#request-hash").textContent = `ID ${next.hash_id}`;
    openModal(modal);
  }

  // Back gesture: close the prompt without declining; it comes back later.
  function dismissContactRequest() {
    const hashId = state.requestCurrent;
    closeModal($("#request-modal"));
    state.requestCurrent = null;
    state.requestQueue = [];
    if (hashId) (state.requestDismissed = state.requestDismissed || new Set()).add(hashId);
  }

  async function answerContactRequest(accept) {
    const hashId = state.requestCurrent;
    if (!hashId) return closeModal($("#request-modal"));
    if (accept) {
      await trustPeer(hashId);
    } else {
      await apiPost(`/api/contact-requests/${encodeURIComponent(hashId)}/decline`, {}, { silent: true });
      state.peers = state.peers.filter((p) => p.hash_id !== hashId);
      renderContacts();
    }
    nextContactRequest();
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

  async function findByIp() {
    const input = $("#find-ip-input");
    const target = (input?.value || "").trim();
    if (!target) {
      toast("Enter the other device's IP address", "error");
      return;
    }
    const btn = $("#find-ip-btn");
    if (btn) btn.disabled = true;
    try {
      const { ok, data } = await apiPost(
        `/api/announce?target=${encodeURIComponent(target)}`, {}, { silent: true }
      );
      if (!ok) {
        toast(data?.error || "Could not reach that address", "error");
        return;
      }
      toast(`Sent a hello to ${target} — looking for a reply…`);
      const found = await waitForDiscoveredAt(target, 6000);
      if (found) {
        closeAddContactModal();
        state.peerTab = "discovered";
        document.querySelectorAll(".peer-tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === "discovered"));
        renderContacts();
        toast(`Found ${found.name} — tap Trust to add them`);
      } else {
        toast(`No reply from ${target}. Is SRLTCP open there, on the same VPN, with UDP 7826 allowed?`, "error");
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function waitForDiscoveredAt(ip, maxMs) {
    const deadline = Date.now() + maxMs;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 700));
      await loadPeers();
      const all = [...state.peers, ...state.trusted];
      const hit = all.find((p) => (p.tcp_host === ip) || (p.address || "").startsWith(`${ip}:`));
      if (hit) return hit;
    }
    return null;
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

  /* ── Pairing (QR code / link / NFC) ── */
  function androidBridge() {
    return window.SRLTCPAndroid || null;
  }

  function androidHas(method) {
    const b = androidBridge();
    return !!(b && typeof b[method] === "function");
  }

  function renderPairQr(uri, box = $("#pair-qr")) {
    if (!box) return;
    box.innerHTML = "";
    if (typeof window.qrcode !== "function") {
      box.textContent = "QR unavailable — use Copy link";
      return;
    }
    try {
      const qr = window.qrcode(0, "M");
      qr.addData(uri);
      qr.make();
      const img = document.createElement("img");
      img.alt = "Pairing QR code";
      img.src = qr.createDataURL(6, 2);
      box.appendChild(img);
    } catch (err) {
      box.textContent = "Code too long for QR — use Copy link";
    }
  }

  async function refreshPairCode() {
    const { ok, data } = await apiGet("/api/pairing/card", { silent: false, fallback: null });
    if (!ok || !data?.uri) {
      toast(data?.error || "Could not create pairing code", "error");
      return;
    }
    state.pairUri = data.uri;
    renderPairQr(data.uri);
    const hashEl = $("#pair-my-hash");
    if (hashEl) hashEl.textContent = (data.hash_id || "").replace(/(.{4})/g, "$1 ").trim();
    const hint = $("#pair-qr-hint");
    if (hint && data.expires) {
      const mins = Math.max(1, Math.round((data.expires * 1000 - Date.now()) / 60000));
      hint.textContent = `Let the other person scan this. Valid for ${mins} minutes, works once.`;
    }
    if (androidHas("setPairingCard")) {
      try { androidBridge().setPairingCard(data.uri); } catch (_) { /* ignore */ }
    }
  }

  function setNfcStatus(text, kind = "info") {
    const el = $("#pair-nfc-status");
    if (!el) return;
    el.textContent = text;
    el.dataset.kind = kind;
    el.classList.toggle("hidden", !text);
  }

  async function openPairModal() {
    const modal = $("#pair-modal");
    if (!modal) return;
    setInputValue("pair-code-input", "");
    setNfcStatus("");
    $("#pair-share")?.classList.toggle("hidden", !androidHas("shareText") && !navigator.share);
    $("#pair-scan")?.classList.toggle(
      "hidden",
      !androidHas("scanQrCode") && !("BarcodeDetector" in window && navigator.mediaDevices)
    );
    const nfcOk = androidHas("isNfcAvailable") && androidBridge().isNfcAvailable();
    $("#pair-nfc")?.classList.add("hidden");
    state.nfcPaired = false;
    openModal(modal);
    await refreshPairCode();
    if (nfcOk) startNfcScan();
  }

  function closePairModal() {
    stopPairCamera();
    if (androidHas("setPairingCard")) {
      try { androidBridge().setPairingCard(""); } catch (_) { /* ignore */ }
    }
    if (androidHas("stopNfcScan")) {
      try { androidBridge().stopNfcScan(); } catch (_) { /* ignore */ }
    }
    closeModal($("#pair-modal"));
  }

  async function importPairCode(code, { source = "link" } = {}) {
    const text = (code || "").trim();
    if (!text) {
      toast("Paste or scan a pairing code first", "error");
      return false;
    }
    const { ok, data } = await apiPost(
      "/api/pairing/import",
      { code: text, in_person: source === "qr" },  // scanned from their screen
      { silent: true }
    );
    if (!ok) {
      toast(data?.error || "That pairing code did not work", "error");
      return false;
    }
    toast(`Paired with ${data.name || data.hash_id?.slice(0, 8)}${source === "nfc" ? " via NFC" : ""}`
      + (data.verified ? " — verified" : ""));
    logActivity(`Paired with ${data.name || data.hash_id?.slice(0, 8)} (${source})`);
    state.peerTab = "trusted";
    document.querySelectorAll(".peer-tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === "trusted"));
    await loadPeers();
    closePairModal();
    selectPeer(data.hash_id, data.name || data.hash_id.slice(0, 8));
    return true;
  }

  // A copied or shared link can reach anyone: pairing through it must not
  // count as "verified in person".
  function markPairLinkShared() {
    if (state.pairUri) apiPost("/api/pairing/shared", { uri: state.pairUri }, { silent: true });
  }

  async function copyPairLink() {
    if (!state.pairUri) return;
    markPairLinkShared();
    try {
      await navigator.clipboard.writeText(state.pairUri);
      toast("Pairing link copied — send it over a channel you trust");
    } catch (_) {
      setInputValue("pair-code-input", state.pairUri);
      toast("Copy failed — the link is in the text box", "error");
    }
  }

  async function sharePairLink() {
    if (!state.pairUri) return;
    markPairLinkShared();
    if (androidHas("shareText")) {
      androidBridge().shareText("My SRLTCP pairing link", state.pairUri);
    } else if (navigator.share) {
      try { await navigator.share({ title: "SRLTCP pairing", text: state.pairUri }); } catch (_) { /* cancelled */ }
    }
  }

  let pairCameraStream = null;
  let pairCameraTimer = null;
  let pairCameraVideo = null;

  function stopPairCamera() {
    if (pairCameraTimer) clearInterval(pairCameraTimer);
    pairCameraTimer = null;
    if (pairCameraStream) pairCameraStream.getTracks().forEach((t) => t.stop());
    pairCameraStream = null;
    pairCameraVideo?.classList.add("hidden");
    pairCameraVideo = null;
  }

  /** Scan a QR code that starts with ``prefix``: the Android scanner, or
   *  the browser's camera in ``video``. Android answers through
   *  window.onAndroidQrResult, which routes the code by its prefix. */
  async function scanQr(video, prefix, onHit) {
    if (androidHas("scanQrCode")) {
      androidBridge().scanQrCode();
      return;
    }
    if (!("BarcodeDetector" in window)) {
      toast("QR scanning is not supported in this browser", "error");
      return;
    }
    stopPairCamera();
    try {
      pairCameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    } catch (_) {
      toast("Camera permission denied", "error");
      return;
    }
    pairCameraVideo = video;
    video.srcObject = pairCameraStream;
    video.classList.remove("hidden");
    await video.play();
    const detector = new window.BarcodeDetector({ formats: ["qr_code"] });
    pairCameraTimer = setInterval(async () => {
      try {
        const codes = await detector.detect(video);
        const hit = codes.find((c) => (c.rawValue || "").startsWith(prefix));
        if (hit) {
          stopPairCamera();
          await onHit(hit.rawValue);
        }
      } catch (_) { /* keep scanning */ }
    }, 400);
  }

  function scanPairQr() {
    if (!androidHas("scanQrCode") && !("BarcodeDetector" in window)) {
      toast("QR scanning is not supported in this browser — paste the link instead", "error");
      return;
    }
    scanQr($("#pair-camera"), "srltcp://pair#", (code) => importPairCode(code, { source: "qr" }));
  }

  function startNfcScan() {
    if (!androidHas("startNfcScan")) return;
    const btn = $("#pair-nfc");
    const result = String(androidBridge().startNfcScan());
    if (result === "off") {
      setNfcStatus("NFC is turned off. Turn it on to pair by tapping phones.", "error");
      if (btn) {
        btn.textContent = "Open NFC settings";
        btn.dataset.mode = "settings";
        btn.classList.remove("hidden");
      }
      return;
    }
    if (result !== "ok") return;
    if (btn) {
      btn.dataset.mode = "";
      btn.classList.add("hidden");
    }
    setNfcStatus("NFC ready — open this Pair screen on the other phone too and hold the phones back to back.");
  }

  function onNfcButton() {
    const btn = $("#pair-nfc");
    if (btn?.dataset.mode === "settings" && androidHas("openNfcSettings")) {
      androidBridge().openNfcSettings();
      return;
    }
    startNfcScan();
  }

  // Coming back from Android's NFC settings: try again automatically.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && $("#pair-modal")?.classList.contains("open")
        && $("#pair-nfc")?.dataset.mode === "settings") {
      startNfcScan();
    }
  });

  // Called by the Android app (MainActivity) — see SRLTCPBridge.kt.
  window.onAndroidQrResult = (text) => {
    if (!text) return;
    if (text.startsWith(VERIFY_PREFIX)) {
      submitVerifyCode(text);
    } else {
      importPairCode(text, { source: "qr" });
    }
  };

  // ── Verified contacts (safety numbers) ──

  const VERIFY_PREFIX = "srltcp://verify#";

  function contactVerified(hashId) {
    return !!state.trusted.find((p) => p.hash_id === hashId)?.verified;
  }

  function setVerifiedMark(el, verified, name) {
    if (!el) return;
    el.classList.toggle("verified", verified);
    const label = verified
      ? `Verified: you checked ${name || "this contact"}'s keys`
      : `Not verified yet: tap to compare safety numbers with ${name || "this contact"}`;
    el.title = label;
    el.setAttribute("aria-label", label);
  }

  function updateVerifiedMark(hashId) {
    const mark = $("#chat-peer-verified");
    if (!mark) return;
    const peer = !state.selectedGroup && state.trusted.find((p) => p.hash_id === hashId);
    mark.classList.toggle("hidden", !peer);
    if (peer) setVerifiedMark(mark, !!peer.verified, peer.name);
  }

  function setPeerVerified(hashId, verified) {
    const peer = state.trusted.find((p) => p.hash_id === hashId);
    if (peer) peer.verified = verified;
    if (state.selectedPeer === hashId) updateVerifiedMark(hashId);
    if (state.editContactTarget === hashId) renderEditContactVerify(hashId);
  }

  function renderEditContactVerify(hashId) {
    const verified = contactVerified(hashId);
    setVerifiedMark($("#edit-contact-verified"), verified);
    const text = $("#edit-contact-verify-text");
    if (text) text.textContent = verified ? "Verified" : "Not verified yet";
  }

  function renderVerifyInfo(info) {
    const verified = !!info.verified;
    const status = $("#verify-status");
    if (status) {
      status.textContent = !info.available
        ? "Connect with this contact once to see your safety number."
        : verified ? "✓ Verified: these are their real keys." : "Not verified yet.";
      status.classList.toggle("ok", verified);
    }
    const number = $("#verify-number");
    if (number) {
      number.innerHTML = info.available
        ? info.number.split(" ").map((g) => `<span>${escapeHtml(g)}</span>`).join("")
        : "—";
    }
    const qr = $("#verify-qr");
    if (qr) {
      qr.classList.toggle("hidden", !info.available);
      if (info.available) renderPairQr(info.code, qr);
    }
    $("#verify-scan")?.classList.toggle(
      "hidden",
      !info.available || (!androidHas("scanQrCode") && !("BarcodeDetector" in window))
    );
    $("#verify-confirm")?.classList.toggle("hidden", !info.available || verified);
    $("#verify-remove")?.classList.toggle("hidden", !verified);
    setPeerVerified(state.verifyTarget, verified);
  }

  async function openVerify(hashId) {
    const peer = state.trusted.find((p) => p.hash_id === hashId);
    if (!peer) return;
    state.verifyTarget = hashId;
    const nameEl = $("#verify-name");
    if (nameEl) nameEl.textContent = peer.name || hashId.slice(0, 8);
    renderVerifyInfo({ available: false, verified: !!peer.verified });
    openModal($("#verify-modal"));
    const { ok, data } = await apiGet(
      `/api/trusted/${encodeURIComponent(hashId)}/safety`, { fallback: null }
    );
    if (ok && data && state.verifyTarget === hashId) renderVerifyInfo(data);
  }

  function closeVerify() {
    stopPairCamera();
    state.verifyTarget = null;
    closeModal($("#verify-modal"));
  }

  async function postVerify(body) {
    const hashId = state.verifyTarget;
    if (!hashId) return;
    const { ok, data } = await apiPost(
      `/api/trusted/${encodeURIComponent(hashId)}/verify`, body, { silent: true }
    );
    if (!ok) {
      toast(data?.error || "Could not verify", "error");
      return;
    }
    if (state.verifyTarget === hashId) renderVerifyInfo(data);
    else setPeerVerified(hashId, !!data.verified);
    if (data.verified) toast(`${state.trusted.find((p) => p.hash_id === hashId)?.name || "Contact"} is verified`);
  }

  function submitVerifyCode(code) {
    if (!state.verifyTarget) {
      toast("Open the contact's Verify screen first, then scan their code", "error");
      return;
    }
    postVerify({ code });
  }

  function confirmVerify() {
    const name = $("#verify-name")?.textContent || "this contact";
    if (!confirm(`Only confirm if every digit matches the number on ${name}'s phone.`)) return;
    postVerify({ confirm: true });
  }
  window.onAndroidNfcResult = (result) => {
    let r = {};
    try { r = typeof result === "string" ? JSON.parse(result) : result || {}; } catch (_) { r = {}; }
    if (r.error) {
      if (!state.nfcPaired) setNfcStatus(`NFC: ${r.error}`, "error");
      return;
    }
    state.nfcPaired = true;
    setNfcStatus(`Paired with ${r.name || "contact"} via NFC.`, "ok");
    toast(`Paired with ${r.name || "contact"} via NFC`);
    loadPeers();
    if (r.hash_id) {
      closePairModal();
      selectPeer(r.hash_id, r.name || r.hash_id.slice(0, 8));
    }
  };

  async function deleteTrusted(hashId, name) {
    if (!confirm(`Remove ${name} from trusted contacts?\n\nYour chat with them is kept for 24 hours: add them back before then and it carries on. After 24 hours it is deleted for good.`)) return;
    closeContactMenu();
    state.trusted = state.trusted.filter((p) => p.hash_id !== hashId);
    delete state.links[hashId];
    delete state.linkMetrics[hashId];
    if (state.selectedPeer === hashId) {
      state.selectedPeer = null;
      state.selectedName = null;
      state.messageCache = [];
      $("#chat-active")?.classList.add("hidden");
      $("#chat-empty")?.classList.remove("hidden");
      if (isAndroidApp()) setAndroidView("sidebar", { skipHistory: true });
      saveMobileSession();
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

  function openEditContact(hashId) {
    closeContactMenu();
    const peer = state.trusted.find((p) => p.hash_id === hashId);
    if (!peer) {
      toast("Only trusted contacts can be edited — trust or pair first", "error");
      return;
    }
    state.editContactTarget = hashId;
    setInputValue("edit-contact-name", peer.name || "");
    setInputValue("edit-contact-host", peer.tcp_host || "");
    setInputValue("edit-contact-port", String(peer.tcp_port || 7825));
    const alts = (peer.alt_hosts || []).filter(Boolean);
    const altEl = $("#edit-contact-alts");
    if (altEl) {
      altEl.textContent = alts.length ? `Also tried: ${alts.join(", ")}` : "";
      altEl.classList.toggle("hidden", !alts.length);
    }
    renderEditContactVerify(hashId);
    const checkEl = $("#edit-contact-check-result");
    if (checkEl) { checkEl.innerHTML = ""; checkEl.classList.add("hidden"); }
    const adv = $("#edit-contact-modal details");
    if (adv) adv.open = !!(peer.wan_host || peer.connection_mode === "wan");
    openModal($("#edit-contact-modal"));
    setTimeout(() => $("#edit-contact-name")?.focus(), 50);
  }

  function closeEditContact() {
    state.editContactTarget = null;
    closeModal($("#edit-contact-modal"));
  }

  async function saveEditContact() {
    const hashId = state.editContactTarget;
    if (!hashId) return;
    const name = ($("#edit-contact-name")?.value || "").trim();
    const port = parseInt($("#edit-contact-port")?.value || "7825", 10);
    if (!name) {
      toast("Name cannot be empty", "error");
      return;
    }
    if (!(port >= 1 && port <= 65535)) {
      toast("Port must be between 1 and 65535", "error");
      return;
    }
    const body = {
      name,
      tcp_host: ($("#edit-contact-host")?.value || "").trim(),
      tcp_port: port,
    };
    const { ok, data } = await apiPatch(`/api/trusted/${encodeURIComponent(hashId)}`, body, { silent: true });
    if (!ok) {
      toast(data?.error || "Could not save contact", "error");
      return;
    }
    toast("Contact saved");
    closeEditContact();
    if (state.selectedPeer === hashId) {
      state.selectedName = name;
      const nameEl = $("#chat-peer-name");
      if (nameEl) nameEl.textContent = name;
      setAvatar($("#peer-avatar"), name, hashId);
    }
    await loadPeers();
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
    // Only the newest request may render: an older, slower response must not
    // replace messages that arrived since (or another chat's messages).
    const seq = (state.loadSeq = (state.loadSeq || 0) + 1);
    const startedAt = Date.now();
    const filter = state.selectedGroup
      ? `group=${encodeURIComponent(state.selectedGroup)}`
      : `peer=${encodeURIComponent(state.selectedPeer)}`;
    const { ok, data } = await apiGet(`/api/messages?limit=1000&${filter}`, { fallback: [] });
    if (!ok || seq !== state.loadSeq) return;
    const msgs = data || [];
    const filtered = msgs.filter((m) => messageForSelectedPeer(m));
    const have = new Set(filtered.map((m) => m.id));
    // Keep messages pushed over the WebSocket while this request was in flight.
    for (const m of state.messageCache) {
      const arrived = state.wsArrivals?.get(m.id) || 0;
      if (!have.has(m.id) && arrived >= startedAt && messageForSelectedPeer(m)) filtered.push(m);
    }
    filtered.forEach((m) => syncTransferStateFromMessage(m));
    state.seenMsgIds = state.seenMsgIds || new Set();
    filtered.forEach((m) => state.seenMsgIds.add(m.id));
    if (!messagesEqual(filtered, state.messageCache)) {
      renderMessages(filtered);
    }
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
    const onTor = transport === "tcp" && isTorAddress(activeLinkForPeer(hashId)?.address);
    const label = transport === "serial" ? "SERIAL" : onTor ? "CONNECTING" : "DIRECT";
    badge.textContent = label;
    badge.title = onTor ? "Found over Tor, setting up a direct connection. Tor is only used to find each other: messages, files and calls wait for the direct connection."
      : transport === "serial" ? "Serial / radio link" : "Direct connection, no server in between";
    badge.className = `transport-badge header-badge ${onTor ? "connecting" : transport}`;
    badge.removeAttribute("aria-hidden");
  }

  function refreshPeerStatus(hashId) {
    if (state.selectedGroup && hashId === state.selectedGroup) {
      updateGroupHeader();
      return;
    }
    updateVerifiedMark(hashId);
    const linked = isPeerLinked(hashId) || inTransferCooldown(hashId);
    const latency = formatLatency(hashId);
    updateChatTransportBadge(hashId, linked);
    if ((linked || inTransferCooldown(hashId)) && latency) {
      updatePeerStatus(`Encrypted · Online · ${latency}`);
      setStatusDot("status-dot online");
    } else if (linked || inTransferCooldown(hashId)) {
      updatePeerStatus("Encrypted · Online");
      setStatusDot("status-dot online");
    } else if (state.connectPending.has(hashId)) {
      updatePeerStatus("Connecting…");
      setStatusDot("status-dot pending");
    } else {
      updatePeerStatus("Offline — messages will be delivered when they're back");
      setStatusDot("status-dot");
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
      if (!ok) {
        // Offline contacts are normal: messages queue and deliver later.
        if (state.selectedPeer === hashId) refreshPeerStatus(hashId);
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
          refreshPeerStatus(hashId);
        }
      } else if (state.selectedPeer === hashId) {
        refreshPeerStatus(hashId);
      }
      return data;
    })();
    state.connectPending.set(hashId, run);
    try {
      return await run;
    } finally {
      state.connectPending.delete(hashId);
      if (state.selectedPeer === hashId) refreshPeerStatus(hashId);
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
    if (state.selectedGroup) {
      const res = await apiPost(`/api/groups/${encodeURIComponent(state.selectedGroup)}/messages`, { text });
      if (sendBtn) sendBtn.disabled = false;
      if (!res.ok) {
        toast(res.data?.error || "Message not sent", "error");
        if (!input.value) input.value = text;
      }
      if (isAndroidApp()) requestAnimationFrame(() => input.focus());
      return;
    }
    const { ok } = await apiPost("/api/messages", {
      recipient_hash: state.selectedPeer,
      text,
      transport: activeLinkTransport(state.selectedPeer),
    });
    if (sendBtn) sendBtn.disabled = false;
    if (!ok) {
      toast("Message failed — reconnecting…", "error");
      if (!input.value) input.value = text;
      await connectPeer(state.selectedPeer, true);
    }
    if (isAndroidApp() && input) {
      requestAnimationFrame(() => input.focus());
    }
  }

  // Photos only (not screenshots or graphics, which JPEG would blur; not GIFs,
  // which would lose their animation).
  const SHRINK_TYPES = /^image\/(jpeg|webp|heic|heif)$/i;
  const SHRINK_NAMES = /\.(jpe?g|webp|heic|heif)$/i;  // Android often gives no type
  const PHOTO_MAX_SIDE = 2560;

  /** A JPEG copy of a photo, at most PHOTO_MAX_SIDE on its longest side.
   *  Always re-encoded (even when not smaller): that leaves out all of the
   *  photo's metadata (GPS location, camera, time). A photo this device
   *  can't decode is sent as it is. */
  async function shrinkPhoto(file) {
    if (!SHRINK_TYPES.test(file.type || "") && !SHRINK_NAMES.test(file.name || "")) return file;
    try {
      const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
      const scale = Math.min(1, PHOTO_MAX_SIDE / Math.max(bitmap.width, bitmap.height));
      const w = Math.max(1, Math.round(bitmap.width * scale));
      const h = Math.max(1, Math.round(bitmap.height * scale));
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      canvas.getContext("2d").drawImage(bitmap, 0, 0, w, h);
      bitmap.close?.();
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
      if (!blob) return file;
      const name = `${file.name.replace(/\.[^.]+$/, "") || "photo"}.jpg`;
      return new File([blob], name, { type: "image/jpeg", lastModified: Date.now() });
    } catch {
      return file; // a format this device can't decode: send the original
    }
  }

  /** Upload and send one file. Resolves to "sending", "queued" (the
   *  contact is away; it goes out when they're back) or false. ``quiet``
   *  leaves the progress toasts to the caller (several files at once). */
  async function sendFileToPeer(file, hashId, peerName, { quiet = false } = {}) {
    if (!hashId || !file) return false;
    // Not connected right now is fine: the file waits and goes out when the
    // contact is reachable again, like a message.
    if (!isPeerLinked(hashId)) await connectPeer(hashId, false);
    if (state.settings?.shrink_photos) file = await shrinkPhoto(file);
    if (!quiet) toast(`Uploading ${file.name}…`);
    const form = new FormData();
    form.append("file", file);
    const { ok: upOk, status, data: uploaded, error } = await safeFetch(
      "/api/upload",
      { method: "POST", body: form },
      { fallback: {} }
    );
    if (!upOk || !uploaded?.path) {
      // Say why: "Upload failed" alone can't be acted on.
      const why = uploaded?.error
        || (status === 0
          ? `couldn't read ${file.name} on this device${error?.message ? ` (${error.message})` : ""}. `
            + "If it's still downloading, wait until it's finished, or pick it again from Files"
          : `the app answered ${status}`);
      toast(why.startsWith("Upload failed") ? why : `Upload failed: ${why}`, "error");
      logActivity(`Upload of ${file.name} failed: ${why}`);
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
    const queued = sent?.state === "paused";
    if (quiet) {
      // the caller sums up
    } else if (queued) {
      toast(`${peerName || "Contact"} isn't reachable right now — ${file.name} will be sent when they're back`);
    } else {
      toast(`Sending ${file.name}…`);
    }
    if (state.selectedPeer === hashId) loadMessages();
    return queued ? "queued" : "sending";
  }

  async function sendFiles(files) {
    if (!state.selectedPeer || !files?.length) return;
    if (state.selectedGroup) {
      toast("Files can't be sent to groups yet");
      return;
    }
    await sendDroppedFiles(files, state.selectedPeer, state.selectedName);
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
    if (list.length === 1) {
      await sendFileToPeer(list[0], hashId, peerName);
      return;
    }
    toast(`Uploading ${list.length} files…`);
    let sending = 0;
    let queued = 0;
    for (const file of list) {
      const result = await sendFileToPeer(file, hashId, peerName, { quiet: true });
      if (result === "sending") sending += 1;
      else if (result === "queued") queued += 1;
    }
    if (sending) toast(`Sending ${sending} file${sending === 1 ? "" : "s"}…`);
    if (queued) {
      toast(`${peerName || "Contact"} isn't reachable right now — ${queued} file${queued === 1 ? "" : "s"} will be sent when they're back`);
    }
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

  /* ── Shared folders / files (E2EE) ── */
  const SHARE_ERRORS = {
    password_required: "This share needs a password.",
    wrong_password: "Wrong password.",
    locked: "Too many wrong passwords — try again in 10 minutes.",
    access_denied: "This share has ended or was removed.",
  };

  function shareExpiry(g) {
    if (!g.expires) return "";
    if (g.ttl_preset === "forever") return "until removed";
    const mins = Math.round((g.expires * 1000 - Date.now()) / 60000);
    if (mins < 1) return "ending";
    if (mins < 60) return `${mins} min left`;
    if (mins < 48 * 60) return `${Math.round(mins / 60)} h left`;
    return `${Math.round(mins / 1440)} days left`;
  }

  function setShareTab(tab) {
    state.shareTab = tab;
    document.querySelectorAll(".share-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.shareTab === tab);
    });
    document.querySelectorAll(".share-panel").forEach((p) => {
      p.classList.toggle("hidden", p.dataset.sharePanel !== tab);
    });
  }

  function shareKind() {
    return document.querySelector('input[name="share-kind"]:checked')?.value || "folder";
  }

  function renderShareForm() {
    const kind = shareKind();
    $("#share-folder-field")?.classList.toggle("hidden", kind !== "folder");
    $("#share-files-field")?.classList.toggle("hidden", kind !== "files");
    const groupSel = $("#share-group");
    if (groupSel) {
      const current = groupSel.value || state.shareGroupPreset || "";
      groupSel.innerHTML = '<option value="">Pick contacts below…</option>' +
        (state.groups || []).map((g) =>
          `<option value="${escapeHtml(g.id)}">Group: ${escapeHtml(g.name)}</option>`
        ).join("");
      groupSel.value = (state.groups || []).some((g) => g.id === current) ? current : "";
    }
    const box = $("#share-recipients");
    if (!box) return;
    const contacts = (state.trusted || []).filter((p) => !p.blocked);
    if (!contacts.length) {
      box.innerHTML = '<div class="empty-hint">No contacts yet — pair with someone first.</div>';
      return;
    }
    const chosen = state.shareChosen || new Set();
    box.innerHTML = contacts.map((p) => `
      <label class="share-recipient">
        <input type="checkbox" value="${escapeHtml(p.hash_id)}" ${chosen.has(p.hash_id) ? "checked" : ""} />
        <span>${escapeHtml(p.name || p.hash_id.slice(0, 8))}</span>
        ${isPeerLinked(p.hash_id) ? '<span class="contact-online" title="Connected">●</span>' : '<span class="share-later" title="Gets it when next connected">later</span>'}
      </label>`).join("");
  }

  function renderShareIncoming() {
    const el = $("#share-incoming-list");
    if (!el) return;
    const remote = state.shareGrants.remote || [];
    const count = $("#share-incoming-count");
    if (count) {
      count.textContent = String(remote.length);
      count.classList.toggle("hidden", !remote.length);
    }
    if (!remote.length) {
      el.innerHTML = '<div class="empty-hint">Nothing shared with you right now.</div>';
      $("#share-entries").innerHTML = "";
      $("#share-password-row")?.classList.add("hidden");
      return;
    }
    el.innerHTML = remote.map((g) => {
      const owner = peerByHash(g.owner_hash);
      const active = state.shareListing.grantId === g.grant_id ? " active" : "";
      return `<button type="button" class="share-grant-btn${active}" data-owner="${escapeHtml(g.owner_hash)}" data-grant="${escapeHtml(g.grant_id)}">
        ${g.password ? "🔒 " : ""}${escapeHtml(g.label || "shared")}
        <span class="share-grant-meta">from ${escapeHtml(owner?.name || g.owner_hash.slice(0, 8))} · ${escapeHtml(shareExpiry(g))}${g.max_downloads ? ` · ${g.downloads_remaining ?? g.max_downloads} downloads left` : ""}</span>
      </button>`;
    }).join("");
    el.querySelectorAll(".share-grant-btn").forEach((btn) => {
      btn.addEventListener("click", () => requestShareListing(btn.dataset.owner, btn.dataset.grant));
    });
  }

  function renderShareMine() {
    const el = $("#share-mine-list");
    if (!el) return;
    const local = state.shareGrants.local || [];
    const count = $("#share-mine-count");
    if (count) {
      count.textContent = String(local.length);
      count.classList.toggle("hidden", !local.length);
    }
    if (!local.length) {
      el.innerHTML = '<div class="empty-hint">You aren’t sharing anything.</div>';
      return;
    }
    el.innerHTML = local.map((g) => {
      const recip = peerByHash(g.recipient_hash);
      const dl = g.max_downloads ? `${g.download_count || 0}/${g.max_downloads} downloads` : `${g.download_count || 0} downloads`;
      const status = g.waiting ? "waiting for them to connect" : shareExpiry(g);
      return `<div class="share-grant-item">
        <span>${g.password ? "🔒 " : ""}${escapeHtml(g.label)} → ${escapeHtml(recip?.name || g.recipient_hash.slice(0, 8))}</span>
        <span class="share-grant-meta">${escapeHtml(status)} · ${escapeHtml(dl)}</span>
        <button type="button" class="share-revoke-btn" data-grant="${escapeHtml(g.grant_id)}">Stop sharing</button>
      </div>`;
    }).join("");
    el.querySelectorAll(".share-revoke-btn").forEach((btn) => {
      btn.addEventListener("click", () => revokeShareGrant(btn.dataset.grant));
    });
  }

  function renderShareGrants() {
    renderShareIncoming();
    renderShareMine();
  }

  function renderShareEntries() {
    const el = $("#share-entries");
    if (!el) return;
    const { grantId, entries = [], error = "", loading = false } = state.shareListing;
    $("#share-password-row")?.classList.toggle(
      "hidden",
      !(grantId && ["password_required", "wrong_password"].includes(error))
    );
    if (!grantId) {
      el.innerHTML = "";
      return;
    }
    if (loading) {
      el.innerHTML = '<div class="empty-hint">Opening…</div>';
      return;
    }
    if (error) {
      el.innerHTML = `<div class="empty-hint">${escapeHtml(SHARE_ERRORS[error] || error)}</div>`;
      return;
    }
    if (!entries.length) {
      el.innerHTML = '<div class="empty-hint">This share is empty.</div>';
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

  async function openShareModal(mode = "offer") {
    state.shareListing = { ownerHash: null, grantId: null, entries: [] };
    state.shareChosen = new Set(state.selectedPeer && !state.selectedGroup ? [state.selectedPeer] : []);
    state.shareGroupPreset = state.selectedGroup || "";
    setInputValue("share-folder", state.settings.shared_folder || "");
    setInputValue("share-label", "");
    setInputValue("share-password", "");
    const files = $("#share-files");
    if (files) files.value = "";
    await loadShareGrants();
    renderShareForm();
    renderShareGrants();
    renderShareEntries();
    setShareTab(mode === "browse" ? "incoming" : "new");
    openModal($("#share-modal"));
  }

  function closeShareModal() {
    closeModal($("#share-modal"));
  }

  async function submitShare() {
    const btn = $("#share-submit");
    const recipients = [...document.querySelectorAll("#share-recipients input:checked")].map((i) => i.value);
    const groupId = $("#share-group")?.value || "";
    if (!recipients.length && !groupId) {
      toast("Choose who to share with", "error");
      return;
    }
    const kind = shareKind();
    let path = "";
    if (kind === "files") {
      const picked = [...($("#share-files")?.files || [])];
      if (!picked.length) {
        toast("Pick at least one file", "error");
        return;
      }
      const form = new FormData();
      picked.forEach((f) => form.append("file", f, f.name));
      if (btn) btn.disabled = true;
      toast(`Preparing ${picked.length} file${picked.length === 1 ? "" : "s"}…`);
      const { ok, data } = await safeFetch("/api/share/files", { method: "POST", body: form }, { fallback: {} });
      if (btn) btn.disabled = false;
      if (!ok || !data?.path) {
        toast(data?.error || "Could not prepare the files", "error");
        return;
      }
      path = data.path;
    } else {
      path = ($("#share-folder")?.value || "").trim();
    }
    if (btn) btn.disabled = true;
    const { ok, data } = await apiPost("/api/share/peer/offer", {
      recipients,
      group_id: groupId,
      kind,
      path,
      label: ($("#share-label")?.value || "").trim(),
      ttl_preset: $("#share-ttl")?.value || "1d",
      download_limit_preset: $("#share-download-limit")?.value || "unlimited",
      password: $("#share-password")?.value || "",
    });
    if (btn) btn.disabled = false;
    if (!ok) {
      toast(data?.error || "Sharing failed", "error");
      return;
    }
    const grants = data.grants || [];
    const waiting = grants.filter((g) => g.waiting).length;
    toast(`Shared with ${grants.length} contact${grants.length === 1 ? "" : "s"}${waiting ? ` (${waiting} will get it when they connect)` : ""}`);
    setInputValue("share-password", "");
    await loadShareGrants();
    renderShareGrants();
    setShareTab("mine");
  }

  async function requestShareListing(ownerHash, grantId, password = null) {
    state.shareListing = { ownerHash, grantId, entries: [], loading: true };
    renderShareIncoming();
    renderShareEntries();
    const body = { owner_hash: ownerHash, grant_id: grantId };
    if (password !== null) body.password = password;
    const { ok, data } = await apiPost("/api/share/peer/list", body, { silent: true });
    if (!ok) {
      state.shareListing = { ownerHash, grantId, entries: [], error: data?.error || "Could not open the share" };
      renderShareEntries();
      return;
    }
    state.shareListing = {
      ownerHash: data.owner_hash || ownerHash,
      grantId: data.grant_id || grantId,
      entries: Array.isArray(data.entries) ? data.entries : [],
      error: data.error || "",
    };
    renderShareEntries();
    if (state.shareListing.error === "password_required" || state.shareListing.error === "wrong_password") {
      $("#share-open-password")?.focus();
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
    if (!grantId || !confirm("Stop sharing? They lose access right away.")) return;
    const { ok, data } = await apiPost("/api/share/peer/revoke", { grant_id: grantId });
    if (!ok) {
      toast(data?.error || "Could not stop sharing", "error");
      return;
    }
    toast("Sharing stopped");
    await loadShareGrants();
    renderShareGrants();
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
      const android = isAndroidApp();
      const inUse = state.transportStatus?.serial?.active ? state.transportStatus.serial.port : "";
      const inUseName = ports.find((p) => p.device === inUse)?.description || inUse;
      // "Automatic" (empty value): the adapter plugged in most recently.
      const auto = android
        ? `Automatic — the adapter plugged in${inUseName ? ` (now: ${inUseName})` : ""}`
        : "Automatic (default port)";
      portEl.innerHTML = `<option value="" ${selectedPort ? "" : "selected"}>${escapeHtml(auto)}</option>`
        + ports.map((p) => {
            const access = p.accessible ? "" : android ? " — allow when asked" : " — no permission";
            return `<option value="${escapeHtml(p.device)}" ${p.device === selectedPort ? "selected" : ""}>${escapeHtml(p.description)}${access}</option>`;
          }).join("");
      if (selectedPort && !ports.some((p) => p.device === selectedPort)) {
        portEl.innerHTML += `<option value="${escapeHtml(selectedPort)}" selected>${escapeHtml(selectedPort)} (saved, not plugged in)</option>`;
      }
      $("#btn-serial-forget")?.classList.toggle("hidden", !selectedPort);
      const hintEl = $("#set-serial-hint");
      const serialErr = state.transportStatus?.serial?.error;
      if (hintEl && android) {
        hintEl.textContent = serialErr && $("#set-enable-serial")?.checked
          ? serialErr
          : ports.some((p) => !p.accessible)
            ? "Pick the adapter and save: Android asks once to allow SRLTCP to use it (or accept SRLTCP when you plug it in). It opens by itself then."
            : "USB serial adapters (FTDI, CP210x, CH340, PL2303, Arduino/ESP32 boards) work through the phone's USB port (USB-C OTG). Both sides need serial on and the same baud rate.";
      } else if (hintEl) {
        if (group.needs_relogin && group.group) {
          hintEl.textContent = `You are in '${group.group}' but this session has not picked it up — restart via ./run.sh web or log out/in.`;
        } else if (group.group && !group.in_session && !group.in_account) {
          hintEl.textContent = `Add your user to '${group.group}' (Arch: uucp, Debian/Ubuntu: dialout), log out/in, then restart SRLTCP.`;
        } else if (ports.length && ports.every((p) => !p.accessible)) {
          hintEl.textContent = `Serial device(s) detected but not readable — check ${group.group || "uucp/dialout"} group permissions.`;
        } else {
          hintEl.textContent = "Both sides need serial on with the same baud rate. Only contacts connect over the radio.";
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

  // Forget the saved adapter: back to "Automatic", saved at once.
  $("#btn-serial-forget")?.addEventListener("click", async () => {
    const res = await apiPost("/api/settings", { serial_port: "" });
    if (!res.ok) {
      toast("Couldn't forget the adapter", "error");
      return;
    }
    if (state.settings) state.settings.serial_port = "";
    await fetchStatus();
    await loadSerialSettings("", parseInt($("#set-serial-baud")?.value || "57600", 10));
    toast("Saved adapter forgotten: the one plugged in is used");
  });

  $("#btn-serial-refresh")?.addEventListener("click", async () => {
    await fetchStatus();
    await loadSerialSettings($("#set-serial-port")?.value || state.settings?.serial_port || "",
      parseInt($("#set-serial-baud")?.value || state.settings?.serial_baud || "57600", 10));
  });

  async function loadInterfaces(selectEl, selectedIp) {
    if (!selectEl) return;
    const { ok, data } = await apiGet("/api/interfaces", { fallback: { interfaces: [] } });
    state.interfaces = ok ? (data?.interfaces || []) : [];
    // "Automatic" keeps working when the phone changes networks; pinning an IP
    // only makes sense for multi-homed desktops.
    const opts = ['<option value="">Automatic (recommended)</option>'];
    let found = !selectedIp;
    for (const i of state.interfaces) {
      const sel = i.ip === selectedIp;
      found = found || sel;
      opts.push(`<option value="${escapeHtml(i.ip)}" ${sel ? "selected" : ""}>${escapeHtml(i.label)}</option>`);
    }
    if (!found) {
      opts.push(`<option value="${escapeHtml(selectedIp)}" selected>${escapeHtml(selectedIp)} (not currently present)</option>`);
    }
    selectEl.innerHTML = opts.join("");
    if (!selectedIp) selectEl.value = "";
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
    setCheckbox("set-direct-internet", settings.direct_internet !== false);
    setCheckbox("set-router-port-mapping", settings.router_port_mapping !== false);
    setInputValue("set-stun-servers", settings.stun_servers || "");
    setCheckbox("set-tor-enabled", settings.tor_enabled !== false);
    setCheckbox("set-shrink-photos", !!settings.shrink_photos);
    setSelectValue("set-disappear", String(settings.disappear_seconds || 0));
    refreshPhonePrivacy();
    setInputValue("set-tor-path", settings.tor_path || "");
    setCheckbox("set-wan-expose", !!settings.wan_expose_port);
    setCheckbox("set-enable-serial", !!settings.enable_serial);
    loadSerialSettings(settings.serial_port || "", settings.serial_baud || 57600);
    const tzEl = $("#set-timezone");
    if (tzEl) loadTimezones(tzEl, settings.timezone || "");
    setCheckbox("set-show-clock", settings.show_clock !== false);
    setSelectValue("set-clock-source", settings.clock_source || "system");
    setInputValue("set-ntp-server", settings.ntp_server || "pool.ntp.org");
    document.dispatchEvent(new Event("srltcp-settings-loaded"));
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
      toast(data?.error || "Failed to save settings", "error");
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
      const tcpActive = !!transportStatus.tcp?.active;
      tcpBtn.disabled = !tcpActive;
      tcpBtn.title = tcpActive
        ? "Announce on TCP/LAN"
        : "TCP transport unavailable — restart the node";
    }
    renderTorStatus(transportStatus.tor);
    renderSerialLink(transportStatus.serial);

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
    if (state.peerTab === "groups") {
      renderGroups();
      return;
    }
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

    // The list is rebuilt every few seconds; keep keyboard focus on the same row.
    const focusedHash = document.activeElement?.closest?.("#contacts .contact")?.dataset.hash;
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
        const meta = `${transportBadge(linked ? activeLinkTransport(p.hash_id) : p.transport)}<span class="contact-endpoint">${escapeHtml(endpoint)}${escapeHtml(metricsStr)}</span>`;
        const isRequest = !!p.metadata?.contact_request;
        const trustBtn = state.peerTab === "discovered" && !trustedIds.has(p.hash_id)
          ? `<button type="button" class="contact-trust" data-trust="${p.hash_id}">${isRequest ? "Accept" : "Trust"}</button>` : "";
        // A div, not a <button>: the row contains its own Trust/Accept button and
        // nested buttons are invalid HTML (the browser hoists them out of the row).
        return `<div class="contact${active}" role="button" tabindex="0" data-hash="${p.hash_id}" data-name="${escapeHtml(p.name)}">
          <div class="avatar" style="background:${hashColor(p.hash_id)}22;color:${hashColor(p.hash_id)};border-color:${hashColor(p.hash_id)}44">
            ${escapeHtml(initials(p.name))}
          </div>
          <div class="contact-info">
            <div class="contact-name">${escapeHtml(p.name)}${p.metadata?.contact_request ? '<span class="contact-request-tag">wants to chat</span>' : ""}${p.blocked ? '<span class="contact-request-tag">blocked</span>' : ""}</div>
            <div class="contact-preview">${meta}</div>
          </div>
          <div class="contact-meta">${state.peerTab === "trusted" ? `<button type="button" class="contact-more" data-more="${p.hash_id}" aria-label="Contact options" title="Edit, block, delete…">⋮</button>` : ""}${trustBtn}${linked ? '<span class="contact-online" title="Connected">●</span>' : ""}${unreadCount(p.hash_id) ? `<span class="unread-badge">${unreadCount(p.hash_id) > 99 ? "99+" : unreadCount(p.hash_id)}</span>` : ""}</div>
        </div>`;
      })
      .join("");

    el.querySelectorAll(".contact").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        if (ev.target.closest(".contact-trust")) return;
        const more = ev.target.closest(".contact-more");
        if (more) {
          ev.stopPropagation();
          openContactMenu(btn.dataset.hash, btn.dataset.name, more);
          return;
        }
        selectPeer(btn.dataset.hash, btn.dataset.name);
        closeSidebarMobile();
      });
      btn.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        if (ev.target !== btn) return;
        ev.preventDefault();
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
    if (focusedHash) el.querySelector(`.contact[data-hash="${CSS.escape(focusedHash)}"]`)?.focus();
  }

  function selectPeer(hashId, name) {
    if (!hashId) return;
    state.selectedPeer = hashId;
    state.selectedName = name;
    const switching = state.selectedGroup || state.selectedPeerShown !== hashId;
    state.selectedGroup = null;
    $("#chat-active")?.classList.remove("group-chat");
    state.stickBottom = true;
    clearUnread(hashId);
    if (switching) {
      // Never show the previous chat's messages under the new name.
      state.messageCache = [];
      renderMessages([]);
    }
    state.selectedPeerShown = hashId;
    const input = $("#msg-input");
    if (input) input.placeholder = "Message…";

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
        ? `${formatBytes(offset)} / ${formatBytes(size)} · waiting for connection`
        : `${formatBytes(offset)} / ${formatBytes(size)} · ${pct}%${speedStr}`;
    const progressBar = stateLabel !== "complete" && !cancelled && !failed
      ? `<div class="progress-track chat-progress"><div class="progress-fill" style="width:${pct}%"></div></div>`
      : "";
    const cancelBtn = tid && ACTIVE_TRANSFER_STATES.has(stateLabel)
      ? `<button type="button" class="file-cancel" data-cancel-transfer="${escapeHtml(tid)}" title="Cancel transfer" aria-label="Cancel transfer">✕</button>`
      : "";
    const showDownload = stateLabel === "complete" && downloadUrl;
    const downloadLabel = meta.is_folder_zip ? "Download folder ZIP" : "Download file";
    const downloadLink = showDownload
      ? `<a class="file-download" href="${downloadUrl}" download="${escapeHtml(filename)}" data-save-transfer="${escapeHtml(tid)}">${downloadLabel}</a>`
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
              : progressLine
        }</div>
        ${progressBar}
        ${downloadLink}
      </div>
      ${cancelBtn}
    </div>`;
  }

  function ensureTransferDownloadLink(bubble, transferId, filename) {
    if (!transferId || bubble.querySelector(".file-download")) return;
    const url = `/api/transfers/${encodeURIComponent(transferId)}/file?download=1`;
    const link = document.createElement("a");
    link.className = "file-download";
    link.href = url;
    link.setAttribute("download", filename || "file");
    link.dataset.saveTransfer = transferId;
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

  const ICON_TIMER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2M9 2h6"/></svg>';

  function disappearIcon(m) {
    const at = Number(m.metadata?.expires_at || 0);
    if (!at) return "";
    const when = new Date(at * 1000).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
    return `<span class="bubble-timer" title="Disappears ${escapeHtml(when)}">${ICON_TIMER}</span>`;
  }

  function onMessagesRemoved(ids) {
    const gone = new Set(ids || []);
    if (!gone.size || !state.messageCache) return;
    const left = state.messageCache.filter((m) => !gone.has(m.id));
    if (left.length !== state.messageCache.length) {
      state.messageCache = left;
      renderMessages(left, { preserveScroll: true });
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
    state.stickBottom = true;
    if (el) el.scrollTop = el.scrollHeight;
  }

  // Keep the chat pinned to the newest message unless the user scrolled up:
  // survives images loading late, the keyboard opening and the app resuming.
  (function pinMessagesToBottom() {
    const el = $("#messages");
    if (!el) return;
    state.stickBottom = true;
    const repin = () => { if (state.stickBottom) el.scrollTop = el.scrollHeight; };
    el.addEventListener("scroll", () => {
      if (el.clientHeight > 0) state.stickBottom = isNearBottom(el);
    }, { passive: true });
    el.addEventListener("load", repin, true);
    el.addEventListener("loadedmetadata", repin, true);
    if (window.ResizeObserver) new ResizeObserver(repin).observe(el);
  })();

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
              ? `${formatBytes(offset)} / ${formatBytes(size)} · waiting for connection`
              : `${formatBytes(offset)} / ${formatBytes(size)} · ${pct}%${speed}`;
    }
    if (stateLabel === "complete" || cancelled || failed) {
      bubble.querySelectorAll(".progress-track, .chat-progress, .file-cancel").forEach((el) => el.remove());
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
        // Use the newest state, not the one captured 200 ms ago: a small file
        // finishes in between, and the stale "0 %" must not overwrite "complete".
        const msg = messageForTransfer(tid);
        const current = msg ? mergeTransferMeta(tid, msg.metadata || {}) : patchData;
        refreshTransferBubble(tid, current, { scroll: false });
      });
      state.pendingTransferPatches.clear();
    }, 200);
  }

  const STATUS_HINTS = {
    pending: "Waiting to send — goes out automatically when they are reachable (same Wi-Fi, VPN or a direct internet connection)",
    sent: "Sent — not confirmed yet; resent automatically until they receive it",
    delivered: "Delivered",
    not_trusted: "They have not accepted you yet",
    failed: "Not sent",
  };

  function isTorAddress(address) {
    return (address || "").includes(".onion") || address === "tor";
  }

  function renderMessages(msgs, { preserveScroll = false, scrollToBottom = false } = {}) {
    const el = $("#messages");
    if (!el) return;
    if (scrollToBottom || !preserveScroll) state.stickBottom = true;
    const wasAtBottom = state.stickBottom || (el.clientHeight > 0 && isNearBottom(el));
    state.messageCache = msgs;
    let lastDate = "";
    let html = "";
    // (Connection state shows in the header only, never in the chat.)
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
      const senderLabel = !out && m.metadata?.group_id
        ? `<div class="bubble-sender" style="color:${hashColor(m.sender_hash)}">${escapeHtml(memberName(m.metadata.group_id, m.sender_hash))}</div>`
        : "";
      const body = senderLabel + (isFile ? renderFileBubble(m, out) : escapeHtml(m.text));
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
            ${disappearIcon(m)}
            <span>${formatTime(m.timestamp)}</span>
            <span class="bubble-status ${m.status}"${out ? ` title="${STATUS_HINTS[m.status] || ""}"` : ""}></span>
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

    // Two-finger pinch zooms the image (the rest of the app does not zoom);
    // one finger pans once zoomed; double-tap toggles 1x / 2.5x.
    const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    let pinch = null;
    let lastTap = 0;
    body.addEventListener("touchstart", (e) => {
      if (e.touches.length === 2) {
        endDrag();
        pinch = { d: dist(e.touches), z: state.mediaZoom };
        return;
      }
      if (e.touches.length !== 1) return;
      const now = Date.now();
      if (now - lastTap < 300) {
        state.mediaZoom = state.mediaZoom > 1 ? 1 : 2.5;
        if (state.mediaZoom === 1) state.mediaPan = { x: 0, y: 0 };
        applyMediaZoom();
        lastTap = 0;
        return;
      }
      lastTap = now;
      startDrag(e.touches[0].clientX, e.touches[0].clientY);
    }, { passive: true });
    body.addEventListener("touchmove", (e) => {
      if (pinch && e.touches.length === 2) {
        e.preventDefault();
        state.mediaZoom = Math.min(5, Math.max(1, pinch.z * (dist(e.touches) / pinch.d)));
        if (state.mediaZoom === 1) state.mediaPan = { x: 0, y: 0 };
        applyMediaZoom();
        return;
      }
      if (!state.mediaDragging || e.touches.length !== 1) return;
      e.preventDefault();
      moveDrag(e.touches[0].clientX, e.touches[0].clientY);
    }, { passive: false });
    const endTouch = (e) => {
      if (e.touches.length < 2) pinch = null;
      if (e.touches.length === 0) endDrag();
    };
    body.addEventListener("touchend", endTouch);
    body.addEventListener("touchcancel", endTouch);
    body.addEventListener("wheel", (e) => {
      if (!$("#media-lightbox")?.classList.contains("open")) return;
      e.preventDefault();
      setMediaZoom(e.deltaY < 0 ? 0.2 : -0.2);
    }, { passive: false });
  }

  /* Save a transfer's file: system "Save as" picker on Android, file picker on desktop. */
  async function saveTransferFile(transferId, url, filename) {
    const bridge = window.SRLTCPAndroid;
    if (bridge && typeof bridge.saveTransferFile === "function") {
      const err = bridge.saveTransferFile(transferId);
      if (err) toast(err, "error");
      return;
    }
    await saveDownload(url, filename);
  }

  /** Desktop: save a download through the file picker when there is one. */
  async function saveDownload(url, filename) {
    if (typeof window.showSaveFilePicker === "function") {
      let handle;
      try {
        handle = await window.showSaveFilePicker({ suggestedName: filename || "file" });
      } catch (e) {
        if (e?.name === "AbortError") return;
        handle = null;
      }
      if (handle) {
        try {
          const res = await fetch(url, { credentials: "same-origin" });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const writable = await handle.createWritable();
          await res.body.pipeTo(writable);
          toast(`Saved ${filename || "file"}`);
        } catch (_) {
          toast("Could not save the file", "error");
        }
        return;
      }
    }
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "file";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ── Folder sync (two-way, with one contact) ──

  function syncStatus(s) {
    const who = escapeHtml(s.peer_name || "your contact");
    switch (s.state) {
      case "offered": return `${who} wants to sync this folder with you`;
      case "invited": return `Waiting for ${who} to accept`;
      case "paused": return `Paused · ${s.files} files`;
      case "stopped": return `${who} stopped syncing. Your files stay where they are`;
      default: {
        const busy = s.pending ? `${s.pending} to download` : "up to date";
        const conflicts = s.conflicts ? ` · ${s.conflicts} conflict copies kept` : "";
        return `${s.files} files · ${busy}${conflicts}`;
      }
    }
  }

  function renderSyncs(list) {
    const box = $("#sync-list");
    if (!box) return;
    if (!list.length) {
      box.innerHTML = '<div class="empty-hint">No synced folders with this contact yet.</div>';
      return;
    }
    box.innerHTML = list.map((s) => {
      const id = escapeHtml(s.id);
      let actions = "";
      let extra = "";
      if (s.state === "offered") {
        extra = `<div class="folder-row">
            <input type="text" id="sync-accept-${id}" value="${escapeHtml(s.suggested_root || "")}" readonly />
            <button type="button" class="action-btn small" data-browse="sync-accept-${id}">Browse</button>
          </div>`;
        actions = `<button type="button" class="action-btn small" data-sync-accept="${id}">Accept</button>
          <button type="button" class="action-btn small secondary" data-sync-decline="${id}">Decline</button>`;
      } else if (s.state === "active") {
        actions = `<button type="button" class="action-btn small" data-sync-now="${id}">Sync now</button>
          <button type="button" class="action-btn small secondary" data-sync-pause="${id}">Pause</button>
          <button type="button" class="action-btn small danger" data-sync-stop="${id}">Stop</button>`;
      } else if (s.state === "paused") {
        actions = `<button type="button" class="action-btn small" data-sync-resume="${id}">Resume</button>
          <button type="button" class="action-btn small danger" data-sync-stop="${id}">Stop</button>`;
      } else {
        actions = `<button type="button" class="action-btn small danger" data-sync-stop="${id}">Remove</button>`;
      }
      return `<div class="sync-item">
          <div class="sync-item-head"><span class="sync-item-name">${escapeHtml(s.name)}</span>
            <span class="sync-item-path">${escapeHtml(s.root || "")}</span></div>
          <div class="sync-item-status${s.state === "active" && !s.pending ? " ok" : ""}">${syncStatus(s)}</div>
          ${extra}
          <div class="modal-actions sync-actions">${actions}</div>
        </div>`;
    }).join("");
  }

  async function loadSyncs() {
    if (!state.syncPeer) return;
    const { ok, data } = await apiGet(
      `/api/sync?peer=${encodeURIComponent(state.syncPeer)}`, { fallback: [] }
    );
    if (ok) renderSyncs(Array.isArray(data) ? data : []);
  }

  function openSyncModal(hashId, name) {
    state.syncPeer = hashId;
    const nameEl = $("#sync-peer-name");
    if (nameEl) nameEl.textContent = name || hashId.slice(0, 8);
    setInputValue("sync-new-path", "");
    setInputValue("sync-new-name", "");
    renderSyncs([]);
    openModal($("#sync-modal"));
    loadSyncs();
  }

  function closeSyncModal() {
    state.syncPeer = null;
    closeModal($("#sync-modal"));
  }

  function onSyncInvite(sync) {
    if (!sync?.peer) return;
    const who = sync.peer_name || "A contact";
    toast(`${who} wants to sync the folder “${sync.name}” with you`);
    if (!state.syncPeer) openSyncModal(sync.peer, who);
    else if (state.syncPeer === sync.peer) loadSyncs();
  }

  async function syncAction(url, body = {}, method = "POST") {
    const { ok, data } = method === "DELETE"
      ? await apiDelete(url)
      : await apiPost(url, body, { silent: true });
    if (!ok) toast(data?.error || "That didn't work", "error");
    await loadSyncs();
    return ok;
  }

  $("#sync-list")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const d = btn.dataset;
    const base = (id) => `/api/sync/${encodeURIComponent(id)}`;
    if (d.syncAccept) {
      const path = $(`#sync-accept-${CSS.escape(d.syncAccept)}`)?.value || "";
      if (await syncAction(`${base(d.syncAccept)}/accept`, { path })) toast("Syncing started");
    } else if (d.syncDecline) {
      await syncAction(`${base(d.syncDecline)}/decline`);
    } else if (d.syncNow) {
      await syncAction(`${base(d.syncNow)}/now`);
    } else if (d.syncPause) {
      await syncAction(`${base(d.syncPause)}/pause`, { paused: true });
    } else if (d.syncResume) {
      await syncAction(`${base(d.syncResume)}/pause`, { paused: false });
    } else if (d.syncStop) {
      if (!confirm("Stop syncing this folder? The files stay on both devices.")) return;
      await syncAction(base(d.syncStop), {}, "DELETE");
    }
  });

  $("#sync-new-start")?.addEventListener("click", async () => {
    const path = $("#sync-new-path")?.value || "";
    if (!path) {
      toast("Choose a folder first", "error");
      return;
    }
    const ok = await syncAction("/api/sync", {
      recipient_hash: state.syncPeer, path, name: ($("#sync-new-name")?.value || "").trim(),
    });
    if (ok) {
      setInputValue("sync-new-path", "");
      setInputValue("sync-new-name", "");
      toast("Invitation sent: syncing starts once they accept");
    }
  });
  $("#sync-close")?.addEventListener("click", closeSyncModal);
  setupModalClose($("#sync-modal"), closeSyncModal);

  // ── This phone: app lock, private lock-screen notifications (Android) ──
  // Stored by the Android app itself (it locks before the page exists) and
  // applied at once, not with "Save settings".

  const APP_LOCK_HINT = "Ask for your fingerprint, face or screen lock when SRLTCP is opened, "
    + "and after 30 seconds away. Messages and calls still arrive while it's locked.";

  function refreshPhonePrivacy() {
    const box = $("#phone-privacy");
    if (!box || !androidHas("appLockState")) return;
    box.classList.remove("hidden");
    const bridge = androidBridge();
    const lock = String(bridge.appLockState());
    const toggle = $("#set-app-lock");
    if (toggle) {
      toggle.checked = lock === "on";
      toggle.disabled = false;
    }
    const hint = $("#app-lock-hint");
    if (hint) {
      hint.textContent = lock === "unavailable"
        ? "Set up a screen lock (PIN, pattern, password, fingerprint or face) in Android settings first."
        : APP_LOCK_HINT;
    }
    setCheckbox("set-private-notifications", !!bridge.privateNotifications());
  }

  window.onAppLockChanged = (lockState) => {
    refreshPhonePrivacy();
    if (lockState === "unavailable") {
      toast("Set up a screen lock in Android settings first", "error");
    } else {
      toast(lockState === "on" ? "App lock is on" : "App lock is off");
    }
  };

  // ── Encrypted backup / restore ──

  async function createBackup() {
    const pw = $("#backup-password")?.value || "";
    if (pw.length < 8) {
      toast("Use a backup password of at least 8 characters", "error");
      return;
    }
    if (pw !== ($("#backup-password2")?.value || "")) {
      toast("The two passwords are different", "error");
      return;
    }
    const btn = $("#btn-backup-create");
    if (btn) btn.disabled = true;
    toast("Creating the encrypted backup…");
    const { ok, data } = await apiPost("/api/backup", { password: pw }, { silent: true });
    if (btn) btn.disabled = false;
    if (!ok || !data?.filename) {
      toast(data?.error || "Backup failed", "error");
      return;
    }
    setInputValue("backup-password", "");
    setInputValue("backup-password2", "");
    const bridge = window.SRLTCPAndroid;
    if (bridge && typeof bridge.saveBackup === "function") {
      const err = bridge.saveBackup(data.filename);  // system "Save as"
      if (err) toast(err, "error");
      return;
    }
    await saveDownload(`/api/backup/${encodeURIComponent(data.filename)}`, data.filename);
  }

  function openRestore() {
    const file = $("#restore-file");
    if (file) file.value = "";
    setInputValue("restore-password", "");
    setRestoreStatus("");
    openModal($("#restore-modal"));
  }

  function setRestoreStatus(text, error = false) {
    const el = $("#restore-status");
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("error", error);
    el.classList.toggle("hidden", !text);
  }

  async function submitRestore() {
    const file = $("#restore-file")?.files?.[0];
    const pw = $("#restore-password")?.value || "";
    if (!file || !pw) {
      setRestoreStatus("Pick the backup file and enter its password.", true);
      return;
    }
    if (!confirm("Replace the identity, contacts and chats on this device with the backup's?")) return;
    const btn = $("#restore-submit");
    if (btn) btn.disabled = true;
    setRestoreStatus("Checking the password and restoring…");
    const form = new FormData();
    form.append("password", pw);
    form.append("file", file);
    const { ok, data } = await safeFetch(
      "/api/backup/restore", { method: "POST", body: form }, { fallback: {} }
    );
    if (btn) btn.disabled = false;
    if (!ok || !data?.restored) {
      setRestoreStatus(data?.error || "Restore failed", true);
      return;
    }
    setRestoreStatus(`Restored ${data.name || "your identity"}: ${data.contacts} contacts, `
      + `${data.messages} messages. Restarting SRLTCP…`);
    const bridge = window.SRLTCPAndroid;
    if (data.restart === "app") {
      if (bridge && typeof bridge.restartApp === "function") {
        setTimeout(() => bridge.restartApp(), 1200);
      } else {
        setRestoreStatus("Restored. Close SRLTCP and open it again to finish.");
      }
      return;
    }
    // Desktop: the app restarts itself; reload once it answers again.
    const reload = async (tries) => {
      try {
        const res = await fetch("/api/status", { credentials: "same-origin" });
        if (res.ok) { location.reload(); return; }
      } catch (_) { /* still restarting */ }
      if (tries > 0) setTimeout(() => reload(tries - 1), 1000);
    };
    setTimeout(() => reload(30), 2500);
  }

  window.onAndroidFileSaved = (ok) => {
    toast(ok ? "File saved" : "Could not save the file", ok ? "info" : "error");
  };

  // Phones have no hover: tapping a message's tick explains it.
  document.addEventListener("click", (e) => {
    const tick = e.target.closest?.(".bubble-status[title]");
    if (tick?.title) toast(tick.title);
  });

  document.addEventListener("click", (e) => {
    const link = e.target.closest?.("a.file-download, #media-lightbox-download");
    if (!link) return;
    const href = link.getAttribute("href") || "";
    const tid = link.dataset.saveTransfer
      || (href.match(/\/api\/transfers\/([^/]+)\/file/) || [])[1];
    if (!tid) return;
    e.preventDefault();
    saveTransferFile(decodeURIComponent(tid), href, link.getAttribute("download") || "");
  });

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
        state.pendingTransferPatches.delete(data.id);
        refreshTransferBubble(data.id, mergeTransferMeta(data.id, state.messageCache[idx].metadata), {
          scroll: merged.state === "complete",
        });
        return;
      }
      scheduleTransferPatch(data.id, data);
    } else if (!state.transferReloads.has(data.id)) {
      // Its bubble isn't loaded yet (it just started): fetch the chat once.
      state.transferReloads.add(data.id);
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

  // Alert once per message: file messages are re-sent as their transfer
  // progresses, and receipts re-send the same message with a new status.
  function firstSighting(m) {
    state.seenMsgIds = state.seenMsgIds || new Set();
    if (state.seenMsgIds.has(m.id)) return false;
    state.seenMsgIds.add(m.id);
    if (state.seenMsgIds.size > 5000) state.seenMsgIds = new Set([...state.seenMsgIds].slice(-2500));
    return true;
  }

  function chatHiddenFromUser() {
    const bridge = window.SRLTCPAndroid;
    const androidBg = typeof bridge?.isInBackground === "function" && bridge.isInBackground();
    return document.hidden || androidBg || (isAndroidApp() && state.androidView !== "chat");
  }

  function onNewMessage(m) {
    const fresh = m.metadata?.group_id ? !!m._new : firstSighting(m);
    if (messageForSelectedPeer(m) && fresh && !isOutgoing(m.sender_hash) && chatHiddenFromUser()) {
      // The open chat is not on screen (app in background / on the list).
      const gid = m.metadata?.group_id;
      const title = gid ? groupById(gid)?.name || "Group" : peerByHash(m.sender_hash)?.name || "New message";
      const text = m.msg_type === "text" || m.msg_type === "group_text"
        ? (m.text || "").slice(0, 120) : `Sent a ${m.msg_type}`;
      notifyUser(title, gid ? `${memberName(gid, m.sender_hash)}: ${text}` : text,
        { tag: gid ? `group-${gid}` : `msg-${m.sender_hash}`, silent: !document.hidden && !isAndroidApp() });
      if (isAndroidApp() && state.androidView !== "chat") bumpUnread(gid || m.sender_hash);
    }
    if (!messageForSelectedPeer(m)) {
      const gid = m.metadata?.group_id;
      if (gid) {
        if (m._new && !isOutgoing(m.sender_hash)) {
          bumpUnread(gid);
          const g = groupById(gid);
          notifyUser(g?.name || "Group", `${memberName(gid, m.sender_hash)}: ${(m.text || "").slice(0, 120)}`,
            { tag: `group-${gid}` });
        }
        return;
      }
      if (!isOutgoing(m.sender_hash) && fresh) {
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
    const prevStatus = prev?.status;
    state.wsArrivals = state.wsArrivals || new Map();
    state.wsArrivals.set(m.id, Date.now());
    if (state.wsArrivals.size > 500) state.wsArrivals = new Map([...state.wsArrivals].slice(-200));
    upsertChatMessage(m);
    if (prev && msgMedia === "text" && prev.text === m.text) {
      // Only the tick changed: patch it instead of redrawing the whole chat
      // (a redraw stops playing videos and loses text selection).
      if (prevStatus !== m.status) {
        const tick = document.querySelector(`[data-msg-id="${CSS.escape(m.id)}"] .bubble-status`);
        if (tick) {
          tick.className = `bubble-status ${m.status}`;
          if (isOutgoing(m.sender_hash)) tick.title = STATUS_HINTS[m.status] || "";
          return;
        }
      } else {
        return;
      }
    }

    if (msgMedia === "file" || msgMedia === "image" || msgMedia === "video") {
      if (prev && prevStatus !== m.status) {
        // A file the contact now has: ✓ becomes ✓✓.
        const tick = document.querySelector(`[data-msg-id="${CSS.escape(m.id)}"] .bubble-status`);
        if (tick) {
          tick.className = `bubble-status ${m.status}`;
          if (isOutgoing(m.sender_hash)) tick.title = STATUS_HINTS[m.status] || "";
        }
      }
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

    // Your own message always jumps to the bottom; incoming ones only if you
    // are already reading the latest messages.
    renderMessages(state.messageCache, {
      preserveScroll: !isOutgoing(m.sender_hash),
      scrollToBottom: isOutgoing(m.sender_hash),
    });
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
    const cancelled = { ...(state.transfers[transferId] || {}), id: transferId, state: "cancelled" };
    syncTransferMessage(cancelled);
    patchTransferBubble(transferId, cancelled);
  }

  function pruneTerminalTransfers() {
    Object.keys(state.transfers).forEach((id) => {
      const st = state.transfers[id]?.state;
      if (st && TERMINAL_TRANSFER_STATES.has(st)) {
        delete state.transfers[id];
      }
    });
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

  // Popups closed by the Android back gesture, topmost first. Each entry is
  // [element id, close function].
  const BACK_CLOSERS = [
    ["media-lightbox", () => closeMediaLightbox()],
    ["folder-modal", () => closeModal($("#folder-modal"))],
    ["edit-contact-modal", () => closeEditContact()],
    ["pair-modal", () => closePairModal()],
    ["add-contact-modal", () => closeAddContactModal()],
    ["share-modal", () => closeShareModal()],
    ["network-modal", () => $("#network-close")?.click()],
    ["release-modal", () => closeModal($("#release-modal"))],
    ["request-modal", () => dismissContactRequest()],
    ["group-modal", () => closeModal($("#group-modal"))],
  ];

  function androidNavigateBack() {
    if (!isAndroidApp()) return false;
    // During a call the back gesture must not hang up or leave the call screen.
    if ($("#call-overlay") && !$("#call-overlay").classList.contains("hidden")) return true;
    const menu = $("#contact-menu");
    if (menu && !menu.classList.contains("hidden")) {
      closeContactMenu();
      return true;
    }
    for (const [id, close] of BACK_CLOSERS) {
      if ($(`#${id}`)?.classList.contains("open")) {
        close();
        return true;
      }
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
    if (groupById(saved.selectedPeer)) {
      selectGroup(saved.selectedPeer);
      if (isAndroidApp()) setAndroidView("chat", { skipHistory: true });
      return;
    }
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
      if (state.selectedPeer) {
        // Already showing a chat (e.g. the keyboard resized the window): keep it.
      } else if (saved?.selectedPeer && saved.androidView === "chat") {
        state._restorePeer = saved;
      } else {
        setAndroidView("sidebar", { skipHistory: true });
      }
    }
  }

  /* ── Events ── */
  window.srltcpApp = {
    apiPost,
    toast,
    notifyUser,
    peerByHash,
    getSettings: () => state.settings,
    getSelected: () => (state.selectedPeer
      ? { hash: state.selectedPeer, name: state.selectedName || state.selectedPeer.slice(0, 8) }
      : null),
  };

  $("#btn-add-contact")?.addEventListener("click", openAddContactModal);
  $("#find-ip-btn")?.addEventListener("click", findByIp);
  $("#find-ip-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") findByIp();
  });
  $("#btn-pair")?.addEventListener("click", () => { closeSidebarMobile(); openPairModal(); });
  $("#edit-contact-save")?.addEventListener("click", saveEditContact);
  $("#edit-contact-cancel")?.addEventListener("click", closeEditContact);
  $("#edit-contact-delete")?.addEventListener("click", async () => {
    const hashId = state.editContactTarget;
    const peer = state.trusted.find((p) => p.hash_id === hashId);
    if (!hashId) return;
    closeEditContact();
    await deleteTrusted(hashId, peer?.name || hashId.slice(0, 8));
  });
  $("#edit-contact-modal")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT") saveEditContact();
  });
  setupModalClose($("#edit-contact-modal"), closeEditContact);
  for (const id of ["#chat-peer-name", "#peer-avatar"]) {
    $(id)?.addEventListener("click", () => {
      if (state.selectedGroup) {
        openGroupModal(state.selectedGroup);
      } else if (state.selectedPeer && state.trusted.some((p) => p.hash_id === state.selectedPeer)) {
        openEditContact(state.selectedPeer);
      }
    });
  }
  $("#chat-peer-verified")?.addEventListener("click", () => {
    if (state.selectedPeer && !state.selectedGroup) openVerify(state.selectedPeer);
  });
  $("#edit-contact-verify")?.addEventListener("click", () => {
    const hashId = state.editContactTarget;
    closeEditContact();
    if (hashId) openVerify(hashId);
  });
  $("#verify-scan")?.addEventListener("click", () =>
    scanQr($("#verify-camera"), VERIFY_PREFIX, submitVerifyCode));
  $("#verify-confirm")?.addEventListener("click", confirmVerify);
  $("#verify-remove")?.addEventListener("click", () => postVerify({ verified: false }));
  $("#verify-close")?.addEventListener("click", closeVerify);
  setupModalClose($("#verify-modal"), closeVerify);
  $("#set-app-lock")?.addEventListener("change", (e) => {
    if (!androidHas("setAppLock")) return;
    e.target.disabled = true;  // until Android answers (onAppLockChanged)
    androidBridge().setAppLock(e.target.checked);
  });
  $("#set-private-notifications")?.addEventListener("change", (e) => {
    if (!androidHas("setPrivateNotifications")) return;
    androidBridge().setPrivateNotifications(e.target.checked);
    toast(e.target.checked ? "Lock screen: message content hidden" : "Lock screen: message content shown");
  });
  $("#btn-backup-create")?.addEventListener("click", createBackup);
  $("#btn-backup-restore")?.addEventListener("click", openRestore);
  $("#setup-restore")?.addEventListener("click", openRestore);
  $("#restore-submit")?.addEventListener("click", submitRestore);
  $("#restore-cancel")?.addEventListener("click", () => closeModal($("#restore-modal")));
  setupModalClose($("#restore-modal"), () => closeModal($("#restore-modal")));
  $("#pair-close")?.addEventListener("click", closePairModal);
  $("#pair-copy")?.addEventListener("click", copyPairLink);
  $("#pair-share")?.addEventListener("click", sharePairLink);
  $("#pair-refresh")?.addEventListener("click", refreshPairCode);
  $("#pair-import")?.addEventListener("click", () => importPairCode($("#pair-code-input")?.value));
  $("#pair-scan")?.addEventListener("click", scanPairQr);
  $("#pair-nfc")?.addEventListener("click", onNfcButton);
  setupModalClose($("#pair-modal"), closePairModal);
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
    tab.addEventListener("click", () => {
      switchSettingsTab(tab.dataset.tab);
      if (tab.dataset.tab === "serial") $("#btn-serial-refresh")?.click();  // adapters come and go
    });
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

  // Keep the keyboard open when tapping Send. Cancelling touchstart would also
  // cancel the click (the old bug: the arrow did nothing on phones), so the tap
  // is handled on touchend instead and the synthetic mouse events are dropped.
  $("#send-btn")?.addEventListener("mousedown", (e) => e.preventDefault());
  $("#send-btn")?.addEventListener("touchend", (e) => {
    if (e.cancelable) e.preventDefault();
    if (!e.currentTarget.disabled) sendMessage();
  }, { passive: false });
  $("#send-btn")?.addEventListener("click", () => sendMessage());

  $("#msg-input")?.addEventListener("keydown", (e) => {
    // Not while an IME is composing (CJK / some Android keyboards).
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
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
    const files = [...(e.target.files || [])];
    e.target.value = "";
    sendFiles(files);
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
      direct_internet: $("#set-direct-internet")?.checked ?? true,
      router_port_mapping: $("#set-router-port-mapping")?.checked ?? true,
      stun_servers: ($("#set-stun-servers")?.value || "").trim(),
      tor_enabled: $("#set-tor-enabled")?.checked ?? true,
      shrink_photos: $("#set-shrink-photos")?.checked || false,
      disappear_seconds: parseInt($("#set-disappear")?.value || "0", 10),
      tor_path: ($("#set-tor-path")?.value || "").trim(),
      wan_expose_port: $("#set-wan-expose")?.checked || false,
      handshake_protocol: "identity",
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

  $("#messages")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-cancel-transfer]");
    if (!btn || btn.disabled) return;
    e.stopPropagation();
    btn.disabled = true;
    cancelTransfer(btn.dataset.cancelTransfer).finally(() => { btn.disabled = false; });
  });

  $("#settings-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    await saveSettings(settingsPayload("set"), false);
  });

  $("#setup-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const port = parseInt($("#setup-web-port")?.value || "9876", 10);
    await saveSettings(settingsPayload("setup"), true);
    if (String(port) !== location.port) {
      toast(`Restart with --port ${port} to apply new HTTPS port`);
    }
  });

  $("#group-save")?.addEventListener("click", saveGroupModal);
  $("#group-cancel")?.addEventListener("click", () => closeModal($("#group-modal")));
  $("#group-leave")?.addEventListener("click", leaveGroupFromModal);
  setupModalClose($("#group-modal"));
  $("#request-accept")?.addEventListener("click", () => answerContactRequest(true));
  $("#request-decline")?.addEventListener("click", () => answerContactRequest(false));

  document.querySelectorAll(".peer-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.peerTabChosen = true;
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
      case "edit":
      case "rename":
        openEditContact(hashId);
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
      case "share":
        closeContactMenu();
        if (state.selectedPeer !== hashId) selectPeer(hashId, name);
        openShareModal("offer");
        break;
      case "sync":
        closeContactMenu();
        openSyncModal(hashId, name);
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
  $("#share-submit")?.addEventListener("click", submitShare);
  $("#edit-contact-check")?.addEventListener("click", async () => {
    const hashId = state.editContactTarget;
    const out = $("#edit-contact-check-result");
    if (!hashId || !out) return;
    out.classList.remove("hidden");
    out.textContent = "Checking…";
    const { ok, data } = await apiPost(`/api/trusted/${hashId}/diagnose`, {}, { silent: true });
    if (!ok) {
      out.textContent = data?.error || "Check failed";
      return;
    }
    const rows = (data.addresses || []).map((a) =>
      `<li class="${a.ok ? "ok" : "bad"}">${a.ok ? "✓" : "✗"} ${escapeHtml(a.address)} — ${escapeHtml(a.detail)}</li>`
    ).join("");
    const tor = !data.tor?.enabled ? "off" : data.tor.state === "ready"
      ? (data.tor.contact_has_address ? "ready" : "ready (contact's Tor address not known yet)")
      : `${data.tor.state}${data.tor.detail ? ` — ${data.tor.detail}` : ""}`;
    const signIn = androidNetworkState().captive
      ? `<p><strong>This Wi-Fi is waiting for you to sign in</strong> (its sign-in page), so nothing can reach the internet yet. Tap <em>Sign in</em> at the top.</p>`
      : "";
    out.innerHTML = signIn + `<p>${escapeHtml(data.summary || "")}</p>` +
      (rows ? `<ul>${rows}</ul>` : "") +
      `<p class="muted">Now: ${escapeHtml(data.path || "not connected")} · Tor: ${escapeHtml(tor)} · Your addresses: ${escapeHtml((data.my_addresses || []).join(", ") || "none")}</p>` +
      speedHtml(data.speed) +
      (data.report ? `<button type="button" class="btn btn-secondary btn-sm" id="btn-copy-conn-report">Copy connection report</button>
        <small class="muted"> (no IP addresses in it: send it along when something doesn't connect)</small>` : "");
    $("#btn-copy-conn-report")?.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(data.report);
        toast("Report copied");
      } catch (_) {
        window.SRLTCPAndroid?.shareText?.("SRLTCP connection report", data.report);
      }
    });
  });
  // Why a contact on another network isn't on the fast (direct) path.
  function speedHtml(sp) {
    if (!sp) return "";
    const yn = (v) => (v ? "yes" : "no");
    const tips = (sp.tips || []).map((t) => `<li>${escapeHtml(t)}</li>`).join("");
    return `<p><strong>Speed:</strong> ${escapeHtml(sp.reason || "")}</p>` +
      `<p class="muted">Public address here: ${yn(sp.my_public)} · on their side: ${yn(sp.their_public)}` +
      ` · IPv6 here: ${yn(sp.my_ipv6)}` + (sp.symmetric_nat ? " · symmetric NAT here" : "") +
      `</p>` +
      (tips ? `<ul>${tips}</ul>` : "");
  }
  document.querySelectorAll(".share-tab").forEach((b) => {
    b.addEventListener("click", () => setShareTab(b.dataset.shareTab));
  });
  document.querySelectorAll('input[name="share-kind"]').forEach((r) => {
    r.addEventListener("change", renderShareForm);
  });
  $("#share-recipients")?.addEventListener("change", (e) => {
    const box = e.target.closest('input[type="checkbox"]');
    if (!box) return;
    state.shareChosen = state.shareChosen || new Set();
    if (box.checked) state.shareChosen.add(box.value);
    else state.shareChosen.delete(box.value);
  });
  $("#share-files")?.addEventListener("change", () => {
    const n = $("#share-files")?.files?.length || 0;
    const hint = $("#share-files-hint");
    if (hint) hint.textContent = n ? `${n} file${n === 1 ? "" : "s"} selected.` : "Pick one or more files. A private copy is shared, so you can move or edit the originals.";
  });
  const openSharePw = () => {
    const { ownerHash, grantId } = state.shareListing;
    if (ownerHash && grantId) requestShareListing(ownerHash, grantId, $("#share-open-password")?.value || "");
    setInputValue("share-open-password", "");
  };
  $("#share-open-submit")?.addEventListener("click", openSharePw);
  $("#share-open-password")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") openSharePw();
  });
  setupModalClose($("#share-modal"), closeShareModal);
  $("#media-lightbox-close")?.addEventListener("click", closeMediaLightbox);
  setupModalClose($("#media-lightbox"), closeMediaLightbox);
  $("#media-zoom-in")?.addEventListener("click", () => setMediaZoom(0.25));
  $("#media-zoom-out")?.addEventListener("click", () => setMediaZoom(-0.25));
  $("#media-zoom-reset")?.addEventListener("click", resetMediaZoom);

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

  // Back from the background: show what arrived meanwhile, newest at the bottom.
  function onAppResumed() {
    if (!state.selectedPeer) return;
    scrollMessagesToBottom();
    loadMessages().then(() => requestAnimationFrame(scrollMessagesToBottom));
  }
  window.srltcpOnResume = () => {
    onAppResumed();
    showNetworkState(androidNetworkState());
  };

  /* Wi-Fi waiting for its sign-in page (planes, hotels): Android knows; say so. */
  function androidNetworkState() {
    try {
      return JSON.parse(window.SRLTCPAndroid?.networkState?.() || "{}") || {};
    } catch {
      return {};
    }
  }
  function showNetworkState(st) {
    $("#wifi-signin")?.classList.toggle("hidden", !st?.captive);
  }
  window.onNetworkState = showNetworkState;
  $("#btn-wifi-signin")?.addEventListener("click", () => window.SRLTCPAndroid?.openWifiSettings?.());
  showNetworkState(androidNetworkState());

  document.addEventListener("visibilitychange", () => {
    saveMobileSession();
    if (!document.hidden) onAppResumed();
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