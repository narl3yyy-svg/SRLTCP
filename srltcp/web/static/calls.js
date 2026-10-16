/* SRLTCP voice & video calls.
 *
 * WebRTC (DTLS-SRTP). Call setup (SDP offer/answer + ICE candidates) is
 * relayed through the existing end-to-end encrypted SRLTCP session, so the
 * DTLS fingerprints are authenticated by the contact's identity key. The media
 * goes through this phone's own local media forwarder (127.0.0.1), which carries
 * it inside the same encrypted session — no third-party signalling, STUN/TURN or relay servers.
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const RING_TIMEOUT_MS = 45000;
  // A network switch (Wi-Fi <-> mobile data) drops the session for a while:
  // the call waits this long for it to come back, then gives up.
  const RECOVER_MS = 90000;
  const RECOVER_EVERY_MS = 3000;

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
    lostAt: 0,           // when media stopped (0 = flowing)
    recoverTimer: null,
    restarting: false,
    facing: "user",      // camera in use: "user" (front) | "environment" (rear)
    switching: false,
  };
  // Answer tapped on the ringing notification, possibly before the offer
  // reached this (freshly opened) page: answer it when it arrives.
  let answerWanted = 0;

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

  // ICE config for one call: this phone's own local media forwarder (on
  // 127.0.0.1). Media then travels inside the encrypted SRLTCP session over
  // the direct connection, so calls work wherever text does; no outside
  // STUN/TURN servers.
  async function iceConfig(peer) {
    try {
      const res = await fetch(`/api/call/ice?peer=${encodeURIComponent(peer)}`, { credentials: "same-origin" });
      if (res.ok) return await res.json();
    } catch (_) { /* fall through */ }
    return { iceServers: [] };
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
    $("#call-switch-camera")?.classList.add("hidden");
    if (call.video && mode !== "incoming") showSwitchIfTwoCameras();
    updateButtons();
  }

  async function showSwitchIfTwoCameras() {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const cams = devices.filter((d) => d.kind === "videoinput").length;
      $("#call-switch-camera")?.classList.toggle("hidden", !(call.video && cams > 1));
    } catch (_) { /* leave it hidden */ }
  }

  /** Front <-> rear camera, without renegotiating: the outgoing track is replaced.
   * Most phones can't open two cameras at once, so the old one closes first. */
  async function openCamera(facing) {
    try {
      return await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { exact: facing }, width: { ideal: 1280 }, height: { ideal: 720 } },
      });
    } catch (_) {
      return navigator.mediaDevices.getUserMedia({ video: { facingMode: facing } });
    }
  }

  async function switchCamera() {
    if (!call.pc || !call.local || call.switching) return;
    call.switching = true;
    const want = call.facing === "user" ? "environment" : "user";
    const old = call.local.getVideoTracks()[0];
    const sender = call.pc.getSenders().find((s) => s.track === old)
      || call.pc.getSenders().find((s) => s.track?.kind === "video");
    if (old) {
      old.stop();
      call.local.removeTrack(old);
    }
    let facing = want;
    let stream = null;
    try {
      stream = await openCamera(want);
    } catch (err) {
      app().toast?.(`Could not switch camera: ${err?.message || err?.name || "error"}`, "error");
      facing = call.facing;
      try { stream = await openCamera(facing); } catch (_) { /* no camera now */ }
    }
    const track = stream?.getVideoTracks()[0];
    if (track) {
      track.enabled = !call.cameraOff;
      call.local.addTrack(track);
      try { await sender?.replaceTrack(track); } catch (_) { /* keeps the old sender state */ }
      call.facing = facing;
    }
    call.switching = false;
    $("#call-overlay")?.classList.toggle("rear-camera", call.facing === "environment");
    const lv = $("#call-local-video");
    if (lv) {
      lv.srcObject = null;
      lv.srcObject = call.local;
      lv.play?.().catch(() => {});
    }
    $("#call-switch-camera")?.setAttribute("aria-label",
      call.facing === "user" ? "Switch to the rear camera" : "Switch to the front camera");
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
    if (!call.startedAt) call.startedAt = Date.now();
    clearInterval(call.clockTimer);
    call.clockTimer = setInterval(() => {
      const s = Math.floor((Date.now() - call.startedAt) / 1000);
      setStatus(`${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} · end-to-end encrypted`);
    }, 1000);
  }

  function reset() {
    clearTimeout(call.ringTimer);
    clearInterval(call.clockTimer);
    clearInterval(call.recoverTimer);
    try { call.pc?.close(); } catch (_) { /* ignore */ }
    call.local?.getTracks().forEach((t) => t.stop());
    Object.assign(call, {
      id: null, peer: null, peerName: "", video: false, pc: null, local: null,
      remote: null, role: null, state: "idle", pendingOffer: null, pendingIce: [],
      ringTimer: null, muted: false, cameraOff: false, speaker: false,
      startedAt: 0, lostAt: 0, recoverTimer: null, restarting: false, facing: "user", switching: false,
    });
    $("#call-overlay")?.classList.remove("rear-camera");
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
    // Older WebViews report only iceConnectionState: map it.
    const state = () => pc.connectionState
      || ({ checking: "connecting", completed: "connected" }[pc.iceConnectionState] ?? pc.iceConnectionState);
    pc.onconnectionstatechange = pc.oniceconnectionstatechange = () => {
      if (pc !== call.pc) return;
      const st = state();
      if (st === "connected") {
        if (call.lostAt) {
          connectionBack();
        } else if (call.state !== "active") {
          call.state = "active";
          startClock();
          routeAudio(true);
          $("#call-remote-audio")?.play?.().catch(() => {});
          $("#call-remote-video")?.play?.().catch(() => {});
        }
      } else if (st === "failed" || st === "disconnected") {
        if (call.state === "active") {
          connectionLost();
        } else if (st === "failed" && !call.lostAt) {
          app().toast?.(
            "Call could not connect — check that the contact is online (a text message should get ✓✓).",
            "error"
          );
          hangup("failed");
        }
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

  /* ── Surviving a network switch ──
   * The media path itself doesn't change (each side's browser talks to its
   * own node), but while the session reconnects no media flows and WebRTC
   * gives up after ~30 s. So: keep the call, say "Reconnecting…", and once
   * the session is back restart ICE (the caller sends a new offer; the callee
   * asks for one). Up to RECOVER_MS, then the call ends. */
  function connectionLost() {
    if (call.lostAt || !call.pc) return;
    call.lostAt = Date.now();
    clearInterval(call.clockTimer);
    setStatus("Reconnecting…");
    call.recoverTimer = setInterval(recoverTick, RECOVER_EVERY_MS);
  }

  function connectionBack() {
    clearInterval(call.recoverTimer);
    call.recoverTimer = null;
    call.lostAt = 0;
    call.state = "active";
    startClock();
    routeAudio(true);
    $("#call-remote-audio")?.play?.().catch(() => {});
    $("#call-remote-video")?.play?.().catch(() => {});
  }

  function recoverTick() {
    if (!call.pc || !call.lostAt) return;
    if (Date.now() - call.lostAt > RECOVER_MS) {
      app().toast?.("Call lost — the connection didn't come back", "error");
      hangup("failed");
      return;
    }
    if (call.role === "caller") restartIce();
    else signal(call.peer, { type: "restart", call_id: call.id });
  }

  // Wait until the description carries all candidates (they'd be lost if
  // sent separately while the session is still coming back).
  function gathered(pc, ms = 3000) {
    if (pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => {
        if (pc.iceGatheringState === "complete") {
          pc.removeEventListener("icegatheringstatechange", done);
          resolve();
        }
      };
      pc.addEventListener("icegatheringstatechange", done);
      setTimeout(resolve, ms);
    });
  }

  async function freshIceConfig(pc) {
    try {
      const cfg = await iceConfig(call.peer);  // addresses may have changed
      pc.setConfiguration({ ...pc.getConfiguration(), ...cfg });
    } catch (_) { /* keep the old one */ }
  }

  async function restartIce() {
    const pc = call.pc;
    if (!pc || call.restarting || call.role !== "caller") return;
    call.restarting = true;
    try {
      await freshIceConfig(pc);
      const offer = await pc.createOffer({ iceRestart: true });
      await pc.setLocalDescription(offer);
      await gathered(pc);
      if (pc === call.pc) {
        await signal(call.peer, { type: "reoffer", call_id: call.id, sdp: pc.localDescription.sdp });
      }
    } catch (_) {
      /* the session isn't back yet: the next tick tries again */
    } finally {
      call.restarting = false;
    }
  }

  async function onReoffer(sdp) {
    const pc = call.pc;
    if (!pc || call.role !== "callee") return;
    try {
      await freshIceConfig(pc);
      await pc.setRemoteDescription({ type: "offer", sdp });
      await pc.setLocalDescription(await pc.createAnswer());
      await gathered(pc);
      if (pc === call.pc) {
        await signal(call.peer, { type: "answer", call_id: call.id, sdp: pc.localDescription.sdp });
      }
    } catch (_) { /* a newer offer follows */ }
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
    if (answerWanted && Date.now() - answerWanted < RING_TIMEOUT_MS) {
      answerWanted = 0;
      accept(call.video);
    }
  }

  // Android: "Answer" on the ringing notification.
  window.onAndroidAnswerCall = () => {
    if (call.state === "incoming") accept(call.video);
    else if (call.state === "idle") answerWanted = Date.now();
  };

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
        if (call.state === "outgoing") {
          call.state = "connecting";
          setStatus("Connecting…");
        }
        try {
          await call.pc.setRemoteDescription({ type: "answer", sdp: sig.sdp });
        } catch (_) {
          return;  // answer to an older restart offer
        }
        await flushIce();
        break;
      case "reoffer":
        await onReoffer(sig.sdp);
        break;
      case "restart":
        restartIce();
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
    $("#call-switch-camera")?.addEventListener("click", switchCamera);
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
