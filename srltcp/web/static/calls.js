/* SRLTCP voice & video calls.
 *
 * Media flows directly between the two devices over WebRTC (DTLS-SRTP).
 * Call setup (SDP offer/answer + ICE candidates) is relayed through the
 * existing end-to-end encrypted SRLTCP session, so the DTLS fingerprints are
 * authenticated by the contact's identity key — no third-party signalling
 * server, and by default no STUN/TURN server either (LAN / direct only).
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const RING_TIMEOUT_MS = 45000;

  const call = {
    id: null,
    peer: null,
    peerName: "",
    video: false,
    pc: null,
    local: null,
    remote: null,
    role: null,          // "caller" | "callee"
    state: "idle",       // idle | outgoing | incoming | connecting | active
    pendingOffer: null,
    pendingIce: [],
    ringTimer: null,
    startedAt: 0,
    clockTimer: null,
    muted: false,
    cameraOff: false,
  };

  const app = () => window.srltcpApp || {};

  function iceServers() {
    const st = app().getSettings?.() || {};
    const url = (st.call_stun_server || "").trim();
    if (!url) return [];
    const server = { urls: url };
    if (/^turns?:/.test(url) && st.call_turn_username) {
      server.username = st.call_turn_username;
      server.credential = st.call_turn_password || "";
    }
    return [server];
  }

  function newCallId() {
    const a = new Uint8Array(12);
    crypto.getRandomValues(a);
    return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  async function signal(peer, payload) {
    const res = await app().apiPost?.(
      "/api/call/signal",
      { recipient_hash: peer, signal: payload },
      { silent: true }
    );
    return !!res?.ok;
  }

  /* ── UI ── */
  function setStatus(text) {
    const el = $("#call-status");
    if (el) el.textContent = text;
  }

  function showOverlay(mode) {
    const ov = $("#call-overlay");
    if (!ov) return;
    ov.classList.remove("hidden");
    ov.dataset.mode = mode;
    ov.classList.toggle("video", call.video);
    $("#call-peer-name").textContent = call.peerName || "Contact";
    $("#call-incoming-actions")?.classList.toggle("hidden", mode !== "incoming");
    $("#call-active-actions")?.classList.toggle("hidden", mode === "incoming");
    $("#call-toggle-camera")?.classList.toggle("hidden", !call.video);
  }

  function hideOverlay() {
    $("#call-overlay")?.classList.add("hidden");
    const rv = $("#call-remote-video");
    const lv = $("#call-local-video");
    if (rv) rv.srcObject = null;
    if (lv) lv.srcObject = null;
  }

  function startClock() {
    call.startedAt = Date.now();
    clearInterval(call.clockTimer);
    call.clockTimer = setInterval(() => {
      const s = Math.floor((Date.now() - call.startedAt) / 1000);
      setStatus(`${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} · end-to-end encrypted`);
    }, 1000);
  }

  function reset() {
    clearTimeout(call.ringTimer);
    clearInterval(call.clockTimer);
    try { call.pc?.close(); } catch (_) { /* ignore */ }
    call.local?.getTracks().forEach((t) => t.stop());
    Object.assign(call, {
      id: null, peer: null, peerName: "", video: false, pc: null, local: null,
      remote: null, role: null, state: "idle", pendingOffer: null, pendingIce: [],
      ringTimer: null, muted: false, cameraOff: false,
    });
    $("#call-mute")?.classList.remove("active");
    $("#call-toggle-camera")?.classList.remove("active");
    hideOverlay();
  }

  /* ── WebRTC plumbing ── */
  async function getMedia(video) {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("This device/browser does not support calls");
    }
    return navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: video ? { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } } : false,
    });
  }

  function createPeerConnection() {
    const pc = new RTCPeerConnection({ iceServers: iceServers(), bundlePolicy: "max-bundle" });
    pc.onicecandidate = (ev) => {
      if (ev.candidate && call.peer) {
        signal(call.peer, { type: "ice", call_id: call.id, candidate: ev.candidate.toJSON() });
      }
    };
    pc.ontrack = (ev) => {
      if (!call.remote) call.remote = new MediaStream();
      call.remote.addTrack(ev.track);
      const rv = $("#call-remote-video");
      const ra = $("#call-remote-audio");
      if (rv) rv.srcObject = call.remote;
      if (ra) ra.srcObject = call.remote;
    };
    pc.onconnectionstatechange = () => {
      const st = pc.connectionState;
      if (st === "connected") {
        call.state = "active";
        startClock();
      } else if (st === "failed") {
        app().toast?.(
          "Call could not connect. Calls work over WireGuard/VPN or the same network; across mobile networks without a VPN, set a TURN server in Settings → Network.",
          "error"
        );
        hangup("failed");
      } else if (st === "disconnected") {
        setStatus("Reconnecting…");
      }
    };
    return pc;
  }

  function attachLocal(stream) {
    call.local = stream;
    const lv = $("#call-local-video");
    if (lv) {
      lv.srcObject = stream;
      lv.muted = true;
    }
    stream.getTracks().forEach((t) => call.pc.addTrack(t, stream));
  }

  async function flushIce() {
    const queued = call.pendingIce.splice(0);
    for (const c of queued) {
      try { await call.pc.addIceCandidate(c); } catch (_) { /* ignore stale */ }
    }
  }

  /* ── Outgoing ── */
  async function startCall(peer, peerName, video) {
    if (!peer) return;
    if (call.state !== "idle") {
      app().toast?.("Already in a call", "error");
      return;
    }
    Object.assign(call, {
      id: newCallId(), peer, peerName, video, role: "caller", state: "outgoing",
    });
    showOverlay("outgoing");
    setStatus("Starting…");
    try {
      call.pc = createPeerConnection();
      attachLocal(await getMedia(video));
      const offer = await call.pc.createOffer();
      await call.pc.setLocalDescription(offer);
      setStatus("Calling…");
      const sent = await signal(peer, {
        type: "offer", call_id: call.id, video, sdp: call.pc.localDescription.sdp,
      });
      if (!sent) {
        app().toast?.(`${peerName || "Contact"} is not reachable right now`, "error");
        reset();
        return;
      }
      call.ringTimer = setTimeout(() => {
        if (call.state === "outgoing") {
          app().toast?.("No answer");
          hangup("timeout");
        }
      }, RING_TIMEOUT_MS);
    } catch (err) {
      app().toast?.(err?.message || "Could not start the call (microphone/camera permission?)", "error");
      if (call.peer) signal(call.peer, { type: "hangup", call_id: call.id, reason: "error" });
      reset();
    }
  }

  /* ── Incoming ── */
  function onOffer(from, sig) {
    if (call.state !== "idle") {
      signal(from, { type: "busy", call_id: sig.call_id });
      return;
    }
    const peer = app().peerByHash?.(from);
    Object.assign(call, {
      id: sig.call_id, peer: from, peerName: peer?.name || from.slice(0, 8),
      video: !!sig.video, role: "callee", state: "incoming", pendingOffer: sig.sdp,
    });
    showOverlay("incoming");
    setStatus(call.video ? "Incoming video call" : "Incoming voice call");
    app().notifyUser?.(
      call.peerName,
      call.video ? "Incoming video call" : "Incoming voice call",
      { tag: `call-${from}` }
    );
    if (navigator.vibrate) navigator.vibrate([400, 200, 400, 200, 400]);
    call.ringTimer = setTimeout(() => {
      if (call.state === "incoming") decline("timeout");
    }, RING_TIMEOUT_MS);
  }

  async function accept(withVideo) {
    if (call.state !== "incoming") return;
    clearTimeout(call.ringTimer);
    call.state = "connecting";
    call.video = !!withVideo && call.video;
    showOverlay("active");
    setStatus("Connecting…");
    try {
      call.pc = createPeerConnection();
      await call.pc.setRemoteDescription({ type: "offer", sdp: call.pendingOffer });
      await flushIce();
      attachLocal(await getMedia(call.video));
      const answer = await call.pc.createAnswer();
      await call.pc.setLocalDescription(answer);
      await signal(call.peer, { type: "answer", call_id: call.id, sdp: call.pc.localDescription.sdp });
    } catch (err) {
      app().toast?.(err?.message || "Could not answer (microphone/camera permission?)", "error");
      hangup("error");
    }
  }

  function decline(reason = "declined") {
    if (call.peer) signal(call.peer, { type: "hangup", call_id: call.id, reason });
    reset();
  }

  function hangup(reason = "hangup") {
    if (call.peer && call.id) signal(call.peer, { type: "hangup", call_id: call.id, reason });
    reset();
  }

  async function onSignal(evt) {
    const from = evt?.hash_id;
    const sig = evt?.signal || {};
    if (!from || typeof sig !== "object") return;
    if (sig.type === "offer") {
      onOffer(from, sig);
      return;
    }
    if (from !== call.peer || sig.call_id !== call.id) return;
    switch (sig.type) {
      case "answer":
        if (call.role !== "caller" || !call.pc) return;
        clearTimeout(call.ringTimer);
        call.state = "connecting";
        setStatus("Connecting…");
        await call.pc.setRemoteDescription({ type: "answer", sdp: sig.sdp });
        await flushIce();
        break;
      case "ice":
        if (!sig.candidate) return;
        if (call.pc?.remoteDescription) {
          try { await call.pc.addIceCandidate(sig.candidate); } catch (_) { /* ignore */ }
        } else {
          call.pendingIce.push(sig.candidate);
        }
        break;
      case "busy":
        app().toast?.(`${call.peerName} is on another call`);
        reset();
        break;
      case "hangup":
        if (call.state === "incoming" || call.state === "outgoing") {
          app().toast?.(sig.reason === "declined" ? "Call declined" : "Call ended");
        }
        reset();
        break;
      default:
        break;
    }
  }

  function toggleMute() {
    call.muted = !call.muted;
    call.local?.getAudioTracks().forEach((t) => { t.enabled = !call.muted; });
    $("#call-mute")?.classList.toggle("active", call.muted);
  }

  function toggleCamera() {
    call.cameraOff = !call.cameraOff;
    call.local?.getVideoTracks().forEach((t) => { t.enabled = !call.cameraOff; });
    $("#call-toggle-camera")?.classList.toggle("active", call.cameraOff);
  }

  window.srltcpOnCallSignal = onSignal;
  window.srltcpCalls = { startCall, hangup, state: () => call.state };

  document.addEventListener("DOMContentLoaded", bind);
  if (document.readyState !== "loading") bind();
  let bound = false;
  function bind() {
    if (bound) return;
    bound = true;
    const selected = () => app().getSelected?.();
    $("#btn-call-voice")?.addEventListener("click", () => {
      const s = selected();
      if (s) startCall(s.hash, s.name, false);
    });
    $("#btn-call-video")?.addEventListener("click", () => {
      const s = selected();
      if (s) startCall(s.hash, s.name, true);
    });
    $("#call-accept-audio")?.addEventListener("click", () => accept(false));
    $("#call-accept-video")?.addEventListener("click", () => accept(true));
    $("#call-decline")?.addEventListener("click", () => decline());
    $("#call-hangup")?.addEventListener("click", () => hangup());
    $("#call-mute")?.addEventListener("click", toggleMute);
    $("#call-toggle-camera")?.addEventListener("click", toggleCamera);
    window.addEventListener("beforeunload", () => {
      if (call.state !== "idle") hangup();
    });
  }
})();
