/* SRLTCP voice & video calls.
 *
 * WebRTC (DTLS-SRTP). Call setup (SDP offer/answer + ICE candidates) is
 * relayed through the existing end-to-end encrypted SRLTCP session, so the
 * DTLS fingerprints are authenticated by the contact's identity key. The media
 * goes through this node's local TURN relay, which carries it inside the same
 * encrypted session — no third-party signalling or relay servers.
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
    speaker: false,
  };

  // Android app: route call audio like a phone call (earpiece/speaker).
  const nativeAudio = () => {
    const b = window.SRLTCPAndroid;
    return b && typeof b.setCallAudio === "function" ? b : null;
  };
  // Answered (connecting) or connected; never while it's still ringing.
  const inCall = () => call.state === "connecting" || call.state === "active";
  function routeAudio(active) {
    try { nativeAudio()?.setCallAudio(!!active, !!call.speaker); } catch (_) { /* ignore */ }
  }
  // Android app: while a call rings or runs it may show over the lock screen;
  // the phone itself rings (notification with the phone's ringtone).
  function callScreen(on) {
    const b = window.SRLTCPAndroid;
    try { if (typeof b?.setCallScreen === "function") b.setCallScreen(!!on); } catch (_) { /* ignore */ }
  }
  function stopRinging() {
    const b = window.SRLTCPAndroid;
    try { if (typeof b?.stopRinging === "function") b.stopRinging(); } catch (_) { /* ignore */ }
  }

  const app = () => window.srltcpApp || {};

  // ICE candidates can overtake the offer (separate requests). Keep them per
  // call id for a short while and hand them over when the offer arrives.
  const earlyIce = new Map();
  function stashEarlyIce(from, sig) {
    const key = `${from}:${sig.call_id}`;
    const list = earlyIce.get(key) || [];
    if (list.length < 64) list.push(sig.candidate);
    earlyIce.set(key, list);
    setTimeout(() => earlyIce.delete(key), 30000);
  }

  // ICE config for one call. Normally our node's local relay: media then
  // travels inside the encrypted SRLTCP session, so calls work wherever text
  // does (LAN, VPN, hub) without depending on WebRTC finding a direct path.
  async function iceConfig(peer) {
    try {
      const res = await fetch(`/api/call/ice?peer=${encodeURIComponent(peer)}`, { credentials: "same-origin" });
      if (res.ok) return await res.json();
    } catch (_) { /* fall through */ }
    const st = app().getSettings?.() || {};
    const url = (st.call_stun_server || "").trim();
    const servers = [];
    if (url) {
      const server = { urls: url };
      if (/^turns?:/.test(url) && st.call_turn_username) {
        server.username = st.call_turn_username;
        server.credential = st.call_turn_password || "";
      }
      servers.push(server);
    }
    return { iceServers: servers };
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
    const avatar = $("#call-avatar");
    if (avatar) avatar.textContent = (call.peerName || "?").trim().charAt(0).toUpperCase() || "?";
    $("#call-incoming-actions")?.classList.toggle("hidden", mode !== "incoming");
    $("#call-active-actions")?.classList.toggle("hidden", mode === "incoming");
    // A voice call can only be answered with voice; a video call offers both.
    $("#call-accept-video")?.classList.toggle("hidden", !call.video);
    $("#call-toggle-camera")?.classList.toggle("hidden", !call.video);
    $("#call-speaker")?.classList.toggle("hidden", !nativeAudio());
    updateButtons();
  }

  function updateButtons() {
    const set = (sel, off, label) => {
      const b = $(sel);
      if (!b) return;
      b.classList.toggle("off", off);
      b.setAttribute("aria-pressed", off ? "true" : "false");
      if (label) b.setAttribute("aria-label", label);
    };
    set("#call-mute", call.muted, call.muted ? "Unmute microphone" : "Mute microphone");
    set("#call-toggle-camera", call.cameraOff, call.cameraOff ? "Turn camera on" : "Turn camera off");
    const spk = $("#call-speaker");
    if (spk) {
      spk.classList.toggle("active", call.speaker);
      spk.setAttribute("aria-pressed", call.speaker ? "true" : "false");
    }
  }

  function hideOverlay() {
    $("#call-overlay")?.classList.add("hidden");
    $("#call-overlay")?.classList.remove("remote-playing", "local-on");
    const ra = $("#call-remote-audio");
    if (ra) ra.srcObject = null;
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
      ringTimer: null, muted: false, cameraOff: false, speaker: false,
    });
    routeAudio(false);
    callScreen(false);
    updateButtons();
    hideOverlay();
  }

  /* ── WebRTC plumbing ── */
  const AUDIO = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  const onWindows = /Windows/i.test(navigator.userAgent || "");

  /** What to tell the user when the browser can't open the camera or microphone. */
  function mediaHelp(err, what) {
    const name = err?.name || "";
    if (name === "NotAllowedError" || name === "SecurityError") {
      return `${what} blocked. Allow it for this page (the icon at the left of the address bar)`
        + (onWindows ? " and in Windows Settings → Privacy & security → Camera / Microphone:"
          + " turn on access and \"Let desktop apps access…\" (browsers count as desktop apps)." : ".");
    }
    if (name === "NotReadableError" || name === "TrackStartError" || name === "AbortError") {
      return `${what} is busy or blocked: close other apps using it (Teams, Zoom, Skype, the Camera app,`
        + " another browser tab)" + (onWindows ? ", check the camera's privacy switch or key on the laptop"
          + " and any antivirus webcam protection" : "") + ", then try again.";
    }
    if (name === "NotFoundError" || name === "DevicesNotFoundError" || name === "OverconstrainedError") {
      return `No ${what.toLowerCase()} found on this device.`;
    }
    return `${what}: ${err?.message || name || "could not be opened"}`;
  }

  /**
   * Microphone (+ camera). The camera is tried with the preferred size, then
   * with anything it offers (some Windows webcam drivers refuse sizes), and if
   * it still can't be opened the call goes ahead with voice only.
   */
  async function getMedia(video) {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error(window.isSecureContext === false
        ? "Calls need a secure page: open SRLTCP at https://… or http://localhost"
        : "This device/browser does not support calls");
    }
    let videoErr = null;
    if (video) {
      const tries = [
        { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } },
        true,
      ];
      for (const v of tries) {
        try {
          return await navigator.mediaDevices.getUserMedia({ audio: AUDIO, video: v });
        } catch (err) {
          videoErr = err;
          if (err?.name === "NotAllowedError" || err?.name === "SecurityError") break;
        }
      }
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: AUDIO, video: false });
    } catch (err) {
      throw new Error(mediaHelp(videoErr && video ? videoErr : err, video ? "Camera/microphone" : "Microphone"));
    }
    if (videoErr) {
      app().toast?.(`Voice only — ${mediaHelp(videoErr, "Camera")}`, "error");
    }
    return stream;
  }

  function createPeerConnection(config) {
    const pc = new RTCPeerConnection({ ...config, bundlePolicy: "max-bundle" });
    pc.onicecandidate = (ev) => {
      if (ev.candidate && call.peer) {
        signal(call.peer, { type: "ice", call_id: call.id, candidate: ev.candidate.toJSON() });
      }
    };
    pc.ontrack = (ev) => {
      if (!call.remote) call.remote = new MediaStream();
      call.remote.addTrack(ev.track);
      // Sound comes from the <audio> element; the <video> stays muted so the
      // voice isn't played twice. play() is explicit: autoplay alone is not
      // enough on Android WebView (it showed a play button and no sound).
      const rv = $("#call-remote-video");
      const ra = $("#call-remote-audio");
      if (rv && rv.srcObject !== call.remote) {
        rv.muted = true;
        rv.srcObject = call.remote;
      }
      if (ra && ra.srcObject !== call.remote) ra.srcObject = call.remote;
      rv?.play?.().catch(() => {});
      ra?.play?.().catch(() => {});
      // Remote audio starts now: apply earpiece/speaker again (the WebView's
      // audio engine can reset the route when it opens its output).
      if (inCall()) routeAudio(true);
    };
    pc.onconnectionstatechange = () => {
      const st = pc.connectionState;
      if (st === "connected") {
        call.state = "active";
        startClock();
        routeAudio(true);
        $("#call-remote-audio")?.play?.().catch(() => {});
        $("#call-remote-video")?.play?.().catch(() => {});
      } else if (st === "failed") {
        app().toast?.(
          "Call could not connect — check that the contact is online (a text message should get ✓✓).",
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
    // Calls always start with the microphone (and camera) on.
    call.muted = false;
    call.cameraOff = false;
    stream.getTracks().forEach((t) => { t.enabled = true; });
    $("#call-overlay")?.classList.add("local-on");
    updateButtons();
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
      speaker: video,
    });
    showOverlay("outgoing");
    setStatus("Starting…");
    try {
      call.pc = createPeerConnection(await iceConfig(peer));
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
      if (call.peer) signal(call.peer, { type: "hangup", call_id: call.id, reason: "no_media" });
      reset();
    }
  }

  /* ── Incoming ── */
  function onOffer(from, sig) {
    // The same offer again (page reopened from the ringing notification).
    if (call.id && call.id === sig.call_id && call.peer === from) return;
    if (call.state !== "idle") {
      signal(from, { type: "busy", call_id: sig.call_id });
      return;
    }
    const peer = app().peerByHash?.(from);
    Object.assign(call, {
      id: sig.call_id, peer: from, peerName: peer?.name || from.slice(0, 8),
      video: !!sig.video, role: "callee", state: "incoming", pendingOffer: sig.sdp,
    });
    const key = `${from}:${sig.call_id}`;
    call.pendingIce.push(...(earlyIce.get(key) || []));
    earlyIce.delete(key);
    showOverlay("incoming");
    setStatus(call.video ? "Incoming video call" : "Incoming voice call");
    if (window.SRLTCPAndroid) {
      callScreen(true);  // the phone rings by itself (call notification)
    } else {
      app().notifyUser?.(
        call.peerName,
        call.video ? "Incoming video call" : "Incoming voice call",
        { tag: `call-${from}` }
      );
      if (navigator.vibrate) navigator.vibrate([400, 200, 400, 200, 400]);
    }
    call.ringTimer = setTimeout(() => {
      if (call.state === "incoming") decline("timeout");
    }, RING_TIMEOUT_MS);
  }

  async function accept(withVideo) {
    if (call.state !== "incoming") return;
    clearTimeout(call.ringTimer);
    stopRinging();
    call.state = "connecting";
    call.video = !!withVideo && call.video;
    call.speaker = call.video;
    showOverlay("active");
    setStatus("Connecting…");
    try {
      call.pc = createPeerConnection(await iceConfig(call.peer));
      await call.pc.setRemoteDescription({ type: "offer", sdp: call.pendingOffer });
      await flushIce();
      attachLocal(await getMedia(call.video));
      const answer = await call.pc.createAnswer();
      await call.pc.setLocalDescription(answer);
      await signal(call.peer, { type: "answer", call_id: call.id, sdp: call.pc.localDescription.sdp });
    } catch (err) {
      app().toast?.(err?.message || "Could not answer (microphone/camera permission?)", "error");
      hangup("no_media");
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
    if (from !== call.peer || sig.call_id !== call.id) {
      if (sig.type === "ice" && sig.candidate && sig.call_id) stashEarlyIce(from, sig);
      return;
    }
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
        if ((call.state === "incoming" || call.state === "outgoing") && sig.reason !== "no_media") {
          app().toast?.(sig.reason === "declined" ? "Call declined" : "Call ended");
        }
        if (sig.reason === "no_media") {
          app().toast?.(`${call.peerName}'s device couldn't open its microphone — the call was`
            + " ended there. Check the camera/microphone permissions on that device.", "error");
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
    updateButtons();
  }

  function toggleCamera() {
    call.cameraOff = !call.cameraOff;
    call.local?.getVideoTracks().forEach((t) => { t.enabled = !call.cameraOff; });
    updateButtons();
  }

  function toggleSpeaker() {
    call.speaker = !call.speaker;
    // Not only once "active": older WebViews never report connectionState.
    if (inCall()) routeAudio(true);
    updateButtons();
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
    $("#call-speaker")?.addEventListener("click", toggleSpeaker);
    // Show the remote picture only once frames arrive (no empty player / play icon).
    const rv = $("#call-remote-video");
    const markPlaying = () => {
      if (rv.videoWidth > 0) $("#call-overlay")?.classList.add("remote-playing");
    };
    rv?.addEventListener("playing", markPlaying);
    rv?.addEventListener("resize", markPlaying);
    rv?.addEventListener("loadeddata", markPlaying);
    window.addEventListener("beforeunload", () => {
      if (call.state !== "idle") hangup();
    });
  }
})();
