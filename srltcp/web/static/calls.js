/* SRLTCP voice & video calls.
 *
 * WebRTC (DTLS-SRTP). Call setup (SDP offer/answer + ICE candidates) is
 * relayed through the existing end-to-end encrypted SRLTCP session, so the
 * DTLS fingerprints are authenticated by the contact's identity key. The media
 * goes through this phone's own local media forwarder (127.0.0.1), which carries
 * it inside the same encrypted session — no third-party signalling, STUN/TURN or relay servers.
 *
 * Over a serial radio (a few KB/s) a call is voice only, at a low bitrate
 * (Opus ~8 kbit/s, 60 ms packets, silence not sent), and each voice packet
 * goes as one unacknowledged radio packet ahead of everything else.
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const RING_TIMEOUT_MS = 45000;
  // A network switch (Wi-Fi <-> mobile data) drops the session for a while:
  // the call waits this long for it to come back, then gives up.
  const RECOVER_MS = 90000;
  const RECOVER_EVERY_MS = 3000;
  // Answered but no media yet after this long: the call ends with a reason
  // instead of showing "Connecting…" for good.
  const CONNECT_TIMEOUT_MS = 45000;
  const RADIO_CONNECT_TIMEOUT_MS = 90000;  // call setup over a radio is slow
  const RADIO_AUDIO_BITRATE = 8000;  // bit/s

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
    narrow: false,       // over a serial radio: low-bitrate voice only
    connectTimer: null,
  };
  // Answer tapped on the ringing notification, possibly before the offer
  // reached this (freshly opened) page: answer it when it arrives.
  let answerWanted = 0;
  const EAR_TOUCH_PX = 40;  // a fingertip is ~10-30 px across; an ear or cheek far more

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

  /** The RTCPeerConnection part of the ICE config, and whether the contact
   *  is reached over a serial radio. */
  function splitIceConfig(cfg) {
    const { narrow, ...rtc } = cfg || {};
    return { rtc, narrow: !!narrow };
  }

  /** SDP for a radio call: Opus at ~8 kbit/s, mono, 60 ms packets (fewer,
   *  bigger packets: less overhead per packet), in-band error correction, and
   *  nothing sent during silence. */
  function narrowAudio(sdp) {
    const pt = /a=rtpmap:(\d+) opus\/48000/i.exec(sdp)?.[1];
    if (!pt) return sdp;
    const ours = /^(maxaveragebitrate|maxplaybackrate|stereo|sprop-stereo|useinbandfec|usedtx)=/i;
    const params = `maxaveragebitrate=${RADIO_AUDIO_BITRATE};maxplaybackrate=16000;stereo=0;`
      + "sprop-stereo=0;useinbandfec=1;usedtx=1";
    const line = `a=fmtp:${pt} `;
    const fmtp = new RegExp(`^a=fmtp:${pt} ([^\\r\\n]*)`, "m");
    let out = sdp.replace(/^a=(max)?ptime:[^\r\n]*\r?\n/gm, "");
    if (fmtp.test(out)) {
      out = out.replace(fmtp, (_whole, existing) => line + [
        ...existing.split(";").map((p) => p.trim()).filter((p) => p && !ours.test(p)),
        params,
      ].join(";"));
    } else {
      out = out.replace(new RegExp(`^(a=rtpmap:${pt} opus/48000[^\\r\\n]*)`, "mi"),
        `$1\r\n${line}${params}`);
    }
    // (Line endings stay CRLF; the new lines go right after the Opus fmtp.)
    return out.replace(fmtp, (whole) => `${whole}\r\na=ptime:60\r\na=maxptime:60`);
  }

  /** Cap our own voice encoder too (the SDP tells the other side); once the
   *  call is negotiated (before that, some WebViews refuse it). */
  async function limitAudioBitrate(pc) {
    for (const sender of pc.getSenders()) {
      if (sender.track?.kind !== "audio") continue;
      try {
        const params = sender.getParameters();
        params.encodings = params.encodings?.length ? params.encodings : [{}];
        params.encodings.forEach((e) => { e.maxBitrate = RADIO_AUDIO_BITRATE; });
        await sender.setParameters(params);
      } catch (_) { /* older WebView: the SDP limit still applies */ }
    }
  }

  async function localDescription(pc, description) {
    if (call.narrow) description = { type: description.type, sdp: narrowAudio(description.sdp) };
    await pc.setLocalDescription(description);
  }

  /** Answered: the call must connect within a while, or it ends with a reason. */
  function startConnectTimer() {
    clearTimeout(call.connectTimer);
    call.connectTimer = setTimeout(() => {
      if (call.state !== "connecting" || call.lostAt) return;
      app().toast?.(call.narrow
        ? "The call couldn't connect over the radio: the radio link is too slow or too busy"
          + " for a call (a file transfer on it also slows calls down)."
        : "Call could not connect — check that the contact is online (a text message should get ✓✓).",
      "error");
      hangup("failed");
    }, call.narrow ? RADIO_CONNECT_TIMEOUT_MS : CONNECT_TIMEOUT_MS);
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
    $("#call-minimize")?.classList.toggle("hidden", mode === "incoming");
    if (mode === "incoming") expand();  // (a ringing call is always full screen)
    if (call.video && mode !== "incoming") showSwitchIfTwoCameras();
    updateButtons();
  }

  async function showSwitchIfTwoCameras() {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const cams = devices.filter((d) => d.kind === "videoinput").length;
      const show = call.video && call.state !== "idle" && call.state !== "incoming" && cams > 1;
      $("#call-switch-camera")?.classList.toggle("hidden", !show);
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

  /* ── Back to the chat while the call goes on ──
   * The call shrinks to a small window over the app (their video, or a pill
   * with the timer for a voice call): drag it anywhere, tap it for full
   * screen, or hang up from it. Android's back gesture does the same. */
  function minimize() {
    const ov = $("#call-overlay");
    if (!ov || ov.classList.contains("hidden") || call.state === "incoming" || call.state === "idle") {
      return false;
    }
    if (!ov.classList.contains("mini")) {
      ov.classList.add("mini");
      ov.setAttribute("aria-modal", "false");
      if (call.peer) app().openChat?.(call.peer, call.peerName);
    }
    return true;
  }

  function expand() {
    const ov = $("#call-overlay");
    if (!ov) return;
    ov.classList.remove("mini");
    ov.style.left = ov.style.top = ov.style.right = ov.style.bottom = "";
    ov.setAttribute("aria-modal", "true");
  }

  const isMini = () => !!$("#call-overlay")?.classList.contains("mini");

  function hideOverlay() {
    expand();
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
    clearTimeout(call.connectTimer);
    clearInterval(call.clockTimer);
    clearInterval(call.recoverTimer);
    try { call.pc?.close(); } catch (_) { /* ignore */ }
    call.local?.getTracks().forEach((t) => t.stop());
    Object.assign(call, {
      id: null, peer: null, peerName: "", video: false, pc: null, local: null,
      remote: null, role: null, state: "idle", pendingOffer: null, pendingIce: [],
      ringTimer: null, muted: false, cameraOff: false, speaker: false,
      startedAt: 0, lostAt: 0, recoverTimer: null, restarting: false, facing: "user", switching: false,
      narrow: false, connectTimer: null,
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
        clearTimeout(call.connectTimer);
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
    // Only now: before the camera is open, browsers list at most one camera
    // (privacy), so a check made earlier hid the button on a phone answering.
    if (call.video) showSwitchIfTwoCameras();
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
      await localDescription(pc, await pc.createOffer({ iceRestart: true }));
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
      await localDescription(pc, await pc.createAnswer());
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
    const { rtc, narrow } = splitIceConfig(await iceConfig(peer));
    if (narrow && video) {
      video = false;
      app().toast?.("Video calls need Wi-Fi or mobile data — calling with voice over the radio");
    }
    Object.assign(call, {
      id: newCallId(), peer, peerName, video, role: "caller", state: "outgoing",
      speaker: video, narrow,
    });
    showOverlay("outgoing");
    setStatus("Starting…");
    try {
      call.pc = createPeerConnection(rtc);
      attachLocal(await getMedia(video));
      await localDescription(call.pc, await call.pc.createOffer());
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
    const { rtc, narrow } = splitIceConfig(await iceConfig(call.peer));
    if (call.state !== "connecting") return;  // hung up meanwhile
    call.narrow = narrow;
    call.video = !!withVideo && call.video && !narrow;  // (no video over a radio)
    call.speaker = call.video;
    showOverlay("active");
    setStatus("Connecting…");
    startConnectTimer();
    try {
      call.pc = createPeerConnection(rtc);
      await call.pc.setRemoteDescription({ type: "offer", sdp: call.pendingOffer });
      await flushIce();
      attachLocal(await getMedia(call.video));
      await localDescription(call.pc, await call.pc.createAnswer());
      if (narrow) await limitAudioBitrate(call.pc);
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
          startConnectTimer();
        }
        try {
          await call.pc.setRemoteDescription({ type: "answer", sdp: sig.sdp });
        } catch (_) {
          return;  // answer to an older restart offer
        }
        if (call.narrow) await limitAudioBitrate(call.pc);
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
  window.srltcpCalls = { startCall, hangup, minimize, expand, isMini, state: () => call.state };

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
    // Phone at your ear (earpiece, not speaker): an ear or cheek touches with a
    // big contact area or several points at once; such touches press nothing.
    // (The Android app also turns the screen off by the proximity sensor.)
    const ov = $("#call-overlay");
    let earUntil = 0;
    const earLike = (e) => e.pointerType === "touch"
      && ((e.width || 0) > EAR_TOUCH_PX || (e.height || 0) > EAR_TOUCH_PX || !e.isPrimary);
    ov?.addEventListener("pointerdown", (e) => {
      if (inCall() && !call.speaker && earLike(e)) earUntil = Date.now() + 800;
    }, true);
    ov?.addEventListener("click", (e) => {
      if (Date.now() < earUntil) {
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);
    $("#call-minimize")?.addEventListener("click", (e) => {
      e.stopPropagation();
      minimize();
    });
    $("#call-mini-hangup")?.addEventListener("click", (e) => {
      e.stopPropagation();
      hangup();
    });
    // The small window: drag it; a tap (no drag) brings the call back.
    let drag = null;
    ov?.addEventListener("pointerdown", (e) => {
      if (!isMini() || e.target.closest("#call-mini-hangup")) return;
      const r = ov.getBoundingClientRect();
      e.preventDefault();  // (dragging the window never selects the chat's text)
      drag = { id: e.pointerId, dx: e.clientX - r.left, dy: e.clientY - r.top, x: e.clientX, y: e.clientY, moved: false };
      ov.setPointerCapture?.(e.pointerId);
    });
    ov?.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      if (!drag.moved && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 8) return;
      drag.moved = true;
      const r = ov.getBoundingClientRect();
      const left = Math.max(4, Math.min(window.innerWidth - r.width - 4, e.clientX - drag.dx));
      const top = Math.max(4, Math.min(window.innerHeight - r.height - 4, e.clientY - drag.dy));
      Object.assign(ov.style, { left: `${left}px`, top: `${top}px`, right: "auto", bottom: "auto" });
    });
    const endDrag = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const tapped = !drag.moved && e.type === "pointerup";
      drag = null;
      if (tapped && isMini() && Date.now() >= earUntil) expand();
    };
    ov?.addEventListener("pointerup", endDrag);
    ov?.addEventListener("pointercancel", endDrag);
    navigator.mediaDevices?.addEventListener?.("devicechange", () => {
      if (call.local && call.video) showSwitchIfTwoCameras();
    });
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
