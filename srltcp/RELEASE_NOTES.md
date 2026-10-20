# SRLTCP Release Notes

## v0.14.2 (2026-10-20)

### Chat during a call
- **Go back to the chat while a call goes on.** Tap **Chat** in the
  top-left corner of the call, or use Android's back gesture. The call
  shrinks to a small window at the top right, and the chat with that
  contact opens, so you can send messages, photos and files, or open other
  chats.
  - A video call keeps showing their video in the window; a voice call
    becomes a small pill with the call timer.
  - Drag the window anywhere. Tap it to bring the call back to full
    screen, or hang up with its red button.
  - A ringing call is always shown full screen, and back never hangs up.

### Fixed
- **The front/rear camera button was sometimes missing on the phone that
  answers a video call** (for example, a laptop calling a phone). The app
  counted the cameras before it had opened one, and until then the
  browser shows only one camera, for privacy. It now counts them once the
  camera is open, and again if a camera is plugged in or removed during
  the call.

### Tested
- Full end-to-end simulation with two real nodes and browsers. The
  answering side, made to behave like a phone with two cameras, shows the
  switch button. The call shrinks to the small window with the video still
  playing, a message sent from the chat meanwhile arrives, and the window
  can be dragged and tapped back to full screen.

## v0.14.1 (2026-10-20)

### Copy and select message text
- **Copy works on the phone.** Long-press a message → *Copy* now uses the
  phone's own clipboard; the web clipboard it used before is often refused
  inside the app. Copying your hash ID, Tor address, a pairing link or a
  connection report uses it too.
- **Select part of a message.** Long-press a message → *Select text*: the
  text is highlighted with the phone's own handles; drag them to the part
  you want and use the phone's *Copy*. On a computer, just drag over the
  text with the mouse. The chat waits to refresh while you're selecting, so
  a new message or a delivery tick doesn't drop your selection.

### Calls with the phone at your ear
- **The screen turns off at your ear**, like the phone app. While call
  audio is on the earpiece, the proximity sensor switches the screen off
  when something is close to it. Then your ear or cheek can't end the
  call, go to the home screen or open the notification panel. It comes back
  on when you move the phone away. On speaker or a headset (and video calls,
  which start on speaker) the screen stays on as before.
- **Backup for phones with a weak proximity sensor:** during a call on the
  earpiece, touches with a large contact area (an ear or cheek rather than
  a fingertip) or several points at once don't press any button.
- Nothing on the call screen can be highlighted any more.

## v0.14.0 (2026-10-20)

### Add a contact on another network without meeting first
- **Your Tor address is under your hash ID.** At the top of the contact
  list, once Tor is connected, a line `Tor: abcdefghij…uvwxyz.onion` shows
  your Tor address, shortened. Tap it to copy the whole address, just like
  the hash ID above it.
- **Add contact takes their Tor address.** *Add contact → Tor address
  (optional)*: paste theirs with their hash ID, and the app rings them over
  Tor straight away. You no longer need to be on the same network once
  first. Both of you add each other: a Tor connection from someone who
  isn't your contact is still refused. Pasted addresses are cleaned up (with
  or without `.onion`, `http://` or a slash) and checked, so a typo is
  caught before saving.
- **Edit contact shows and takes their Tor address** too, so you can fix it
  or paste it in later.
- **New Tor address** (*Settings → Network*, while Tor is connected): if
  your address got around more than you wanted, make a fresh one. Contacts
  you're connected to get it at once; anyone else needs the new one.
- **Privacy:** anyone who has your Tor address can tell when your device is
  online (never its IP address or what you send), so share it like a phone
  number. A wrong address for a contact can't let anyone impersonate them:
  their signed, quantum-safe handshake is still checked.

### Tested
- Network simulation with real Tor (a private Tor network) and NAT routers:
  two new devices on different home networks that never met added each
  other by hash ID and Tor address only. The message arrived over a direct
  connection after 17 s and the reply came back. After *New Tor address*
  the connected contact had the new address within seconds.

## v0.13.9 (2026-10-19)

**Update both phones:** the serial radio's acknowledgements changed format
(they now carry the measured speed), so 0.13.9 and 0.13.8 don't talk over
the radio.

### Serial radio: faster, and connects by itself
- **Plug in the second adapter and the phones connect.** No more switching
  Wi-Fi off and pressing *Apply* on both phones. Each phone now runs its own
  radio loop (every second) and, for 90 s after an adapter is plugged in or
  a radio session drops, calls its contacts every ~4 s at random moments, so
  two radios never keep talking over each other. In simulations, a phone
  whose adapter was plugged in while the app ran was connected in about
  6 s on a cable or SiK telemetry radio, and 17 s on a LoRa bridge (0.13.8
  never connected on the LoRa model).
- **Much closer to the line speed.** The sender now paces itself by the
  speed the receiving phone actually measures (sent back in every
  acknowledgement) instead of guessing. It sends bigger packets when the
  link is clean, fills each turn on the air with several messages, and
  only treats losses at the end of a burst as "too fast". A lost
  acknowledgement no longer resends a whole turn. File pieces on the radio
  are sized to about 6 s of the measured speed (2–24 KB). Simulated link
  speed at 57600 baud (5.76 KB/s line), before → after: cable 3.46 → 3.84
  KB/s, SiK radio 3.01 → 3.39 KB/s, SiK with error correction 1.57 → 1.82
  KB/s, LoRa 1.15 → 1.23 KB/s, noisy link 2.07 → 2.47 KB/s. Whole files in
  the full app: about 3.45 KB/s on a cable and 2.8 KB/s on SiK. The rest of
  the line goes to packet headers, checksums, acknowledgements and taking
  turns on the air.
- **Chat cuts in ahead of a file on the radio.** A chat message during a
  transfer arrived in 0.1–0.4 s on cable and SiK.
- **Live progress on the radio moves smoothly**, from what the link has
  actually delivered rather than once per finished piece.
- **Ping on the radio** is measured by one phone and shared, so both show
  the same figure without doubling the traffic.
- **Turning Wi-Fi off on one phone moves both to the radio within
  seconds.** The other phone checks its Wi-Fi session as soon as the radio
  says the contact has none, and moves over if it doesn't answer in 5 s.
  It then waits 30 s before trying that Wi-Fi path again (at once if its
  own network changes).
- **Fixed: the radio not reconnecting for up to 3 minutes.** When a radio
  session moved on to a shared Wi-Fi right after it came up, the phone
  still counted its radio call as "in progress" for 3 minutes. If the
  Wi-Fi then went away, it didn't call over the radio again until that
  ran out.
- Closing a dead connection can no longer hang the app's next steps: it's
  cut after 2 s.

### Wi-Fi ↔ mobile data
- **Phones behind a strict carrier NAT now connect directly.** Many
  carriers give a new public port for every destination, so a phone on
  Wi-Fi and one on mobile data never found each other. Now a phone that
  sees this (two STUN servers report different ports) tells its contact,
  and the two do a "birthday" punch: 192 extra sockets on the strict side,
  random port probes from the other, all encrypted, for 10 s. In the
  network simulation (a strict NAT on one side, a normal home router on
  the other) the message went over a direct connection after 21 s.
  0.13.8 never connected. Strict NAT on *both* sides still can't be
  crossed without a relay; messages wait, as before.
- **Less "stuck on handshaking" over Tor.** A slow first handshake over
  Tor was replaced by a second one after 10 s, which could leave the two
  phones with different keys. A handshake over Tor now gets 45 s.

### Tested
- Two full app instances over simulated radio modems (cable, SiK, SiK with
  ECC, LoRa, a noisy link; random loss, half duplex, collisions, buffer
  overflow, plugging in while running), several random seeds each.
- Network simulations with real Tor (a private Tor network), NAT routers,
  STUN and packet loss: two homes, strict NAT on one or both sides, carrier
  NAT with a 40 MB file (10.8 MB/s), a VPN phone with a home Wi-Fi phone.
- Results on real hardware depend on the radios and adapters; tell us what
  you see.

## v0.13.8 (2026-10-17)

### Switching between the radio, Wi-Fi and mobile data
- **A file sent over the radio now finishes after you switch to Wi-Fi.**
  Your test showed it: the receiving phone had every byte (132.2 KB of
  132.2 KB) but the file never completed, and the sender stayed at 6 %.
  - The cause: on the new path the sender resent part of the file in
    larger pieces. The check it runs over the file (needed before the file
    counts as finished) skipped those bytes, so "finished" was never sent.
    Every stall then also tore the connection down, which is why the header
    flipped between *Offline* and *DIRECT*.
  - Fixed: the check now covers every byte, however the file was resent.
- **A moving file carries on at once on the new path.** Both phones sync up
  (the receiver says exactly what it has) instead of waiting out a timer
  meant for the slow radio. In a simulation of two phones on 57600-baud
  radios, a transfer moved to Wi-Fi in under 0.1 s (0.13.7: never finished).
- **Wi-Fi takes over from the radio as soon as it's there.** A phone that
  joins a Wi-Fi, a contact that shows up on your Wi-Fi, or new addresses
  from a contact now trigger the switch right away. It used to wait up to
  45 s for the next periodic check.
- **One phone leaving the radio no longer leaves the other one stuck.**
  When only one phone switched its radio off, the other kept its radio
  session and refused the internet connection for up to a minute. Now it
  checks the radio at once: no answer within 5 s, and it moves to the
  internet too. In the simulation both phones were reconnected over the
  internet 7 s after one switched its radio off.
- **Fixed: one phone showing the contact connected and the other not.**
  When a better path came up, a background reconnect could start a second
  handshake on the same connection while the first was being answered. The
  two phones then ended up with different keys, and nothing got through. A
  connection now carries one handshake at a time, and a fast answer to a
  path change is never thrown away as "unexpected".

### Ping over the serial radio
- **Both phones now show the same ping for their link.** Each phone used to
  show only its own measurements, taken at different moments and behind
  different traffic (in the simulation during a transfer: about 760 ms on
  one phone, 440 ms on the other). Each ping now carries the sender's
  figure, and both phones show the average of the two.
- Over the radio the ping keeps updating during file transfers (it used to
  freeze at the value from before the transfer).

### Long-press, like Signal
- **Long-press a message** (right-click on a PC): it lifts above the dimmed
  chat with **Copy**, **Save** (files), **Select**, **Info** and
  **Delete**.
  - **Select:** tap more messages, then Copy or Delete them together.
  - **Info:** when it was sent or received, delivery status, connection,
    file details.
  - Deleting a photo, video or file also deletes the received file, like in
    Signal. Your contact keeps their copy.
- **Long-press a chat in the list** for its menu: *Delete chat…* comes
  first. For groups: *Delete chat…* and *Group info…*.
- The trash button in the chat header is gone.

### Security
A full security review of the protocol, the radio, file transfers and the
web interface found the issues below; each is fixed and has a test showing
the attack no longer works.
- **Encryption counter:** installing the same session keys twice (a
  repeated handshake answer) used to start the message counter over, which
  would reuse AES-GCM nonces under one key. That can no longer happen.
- **Pings are encrypted.** Anyone able to inject packets (for example on the
  radio) could forge answers that made a dead connection look alive and
  faked the ping time. Pings and their answers now travel inside the
  end-to-end encrypted session.
- **Strangers on the radio:** everyone in range shares the radio, and a
  validly signed handshake from any throwaway identity could tear down your
  contact's radio session. Only contacts may open a session on the radio now,
  and a session isn't given up for someone else's handshake.
- **Replayed beacons:** an old beacon, replayed later, could drop a working
  radio session. Beacons older than the newest one from that contact, or
  from before the session started, are ignored.
- **Radio link layer:** forged acknowledgements or packets with a guessed
  message number could make real messages count as delivered while they
  weren't. Answers must now match the message, and a "duplicate" must match
  the content already received.
- **Handshakes check the signature first.** The slow post-quantum work, and
  any decision about the session, now come after the (fast) signature check;
  a connection may open at most 12 handshakes a minute.
- **Deleting files never touches another file.** A cancelled or failed
  download kept the name its file would have had. In 0.13.7, *Delete chat*
  with "Also delete the files received" could then delete a later file that
  got that name. Only a file the app itself finished receiving is ever
  deleted now, and deleting messages only affects the chat they're in.
- **Web interface:** requests that change anything must come from this
  exact page (https, this port): a web page on another port of the same
  computer could otherwise act as you. The access token is no longer written
  to the log, and received files open sandboxed (an SVG can't run script).
- **Limits:** memory for out-of-order file pieces is capped in total, the
  free-space check counts what other downloads will still write, and a
  cancelled "tap to download" file can't be restarted.
- Left for a later version (low risk, need a protocol change): a session
  only proves itself with its first encrypted message on the radio today,
  not on other paths; names and keys in a radio handshake are not hidden.

## v0.13.7 (2026-10-17)

### Scrolling the chat
- **Scrolling up is never pulled back down.** When you started scrolling
  up from the newest message, the chat jumped back to the bottom for the
  first few lines. It also jumped to the bottom when a photo finished
  arriving or you deleted a message while reading older messages.
- **Now:**
  - while your finger (or the fling after it) moves the chat, nothing moves
    it for you;
  - the chat follows new messages again only once you scroll right to the
    bottom yourself, or send a message.

### Delete a chat
- **The 🗑 button in the chat header** (and *Delete chat…* in a contact's ⋮
  menu) deletes every message of the chat from this device, after asking.
  It works in group chats too.
- **"Also delete the files received in this chat"** removes the photos,
  videos and files they sent you from this phone as well. Files you sent
  are never touched.
- Your contact keeps their copy of the chat.
- Deleting a chat with a contact no longer deletes their messages in your
  group chats.

### Photos and videos sent together show as an album
- **Several photos or videos sent in a row** (within 2 minutes) show as one
  grid, like in Signal, instead of one large bubble each.
- Tap a picture to open it full size. Videos show a ▶ and play when tapped.
- While they arrive, each tile shows its own progress and a ✕ to cancel it.

### Large files wait for a tap
- **Settings → General → "Download automatically up to"** (default 2 GB):
  a received file larger than this waits in the chat with a **Download**
  button (✕ declines it). Nothing is stored or downloaded until you tap.
- **"Over the serial radio, up to"** (default 2 MB) is a separate limit for
  the radio, which moves only a few KB per second.
- The sender's phone shows "waiting for them to tap Download". It doesn't
  resend the offer or give up meanwhile, and the file still waits after
  either app restarts.
- *Ask for every file* and *Any size* are also choices.

### Ping
- **Why one phone shows 300 ms and the other 400 ms.** Each phone measures
  its own ping: the time until the other phone answers, at a moment of its
  own choosing.
  - That time includes how busy both phones are just then, and anything
    queued on the connection ahead of the ping (a file piece, an
    acknowledgement, a radio turn).
  - Two measurements taken at different moments differ by 100 ms or more
    without anything being wrong.
- **The ping shown is now the median of the last 5 measurements,** so one
  slow answer no longer makes the number jump. Both phones show steadier,
  closer values.

### STUN servers
- **Leave the field empty.** The built-in defaults work everywhere:
  - `stun.l.google.com:19302`
  - `stun.cloudflare.com:3478`
- A STUN server only tells your phone its public internet address, so two
  phones on different networks can connect directly.
- DNS servers such as 8.8.8.8 or 1.1.1.1 are *not* STUN servers and don't
  answer STUN. The setting now refuses them with an explanation, and the
  hint under the field says what to enter (e.g. `stun.nextcloud.com:443`).

## v0.13.6 (2026-10-17)

### Files over the serial radio: about twice as fast
- **Faster radio transfers.** Your test showed about 500 B/s on radios set
  to 57600 baud, which can carry about 5.7 KB/s. Most of the time was lost
  in the app, not on the air:
  - every file piece took two separate turns on the radio, each waiting for
    an answer;
  - every 64-byte packet was its own USB write followed by a pause;
  - sending was held below the line's speed.
- **Now:**
  - turns of up to 64 packets;
  - packets written in batches;
  - sending at the line's speed (it slows down only if the radio loses
    data);
  - file data in 8 KB pieces.
- In a simulation of two phones on 57600-baud radios (the serial line's
  speed, USB packets, 3% of radio frames lost), a file moved at **3.3 KB/s**
  (0.13.5: 1.8 KB/s). The phones connected in 5 s (0.13.5: 12 s).
- 57600 baud means at most about 5.7 KB/s, and each packet also carries its
  number and a checksum, so about 4.5 KB/s is the ceiling for file data. A
  147 MB video still takes many hours over a radio.
- If both radios can do more, set them and both phones to 115200 baud for
  about twice the speed. Many radios also have a separate *air* speed that
  can be the real limit.
- **Settings → Serial now shows what the radio carries right now**
  (↑ sent / ↓ received per second) and the current sending pace.

### Ping no longer climbs after a cancelled file
- **Fixed: the ping climbed to many seconds after cancelling a file, with
  nothing else going on.** The cancelled file's data was still queued on
  the radio and kept going out for minutes, and pings waited behind it.
- Cancelling a file, pausing it, or resending part of it now drops its
  queued data at once.
- During a transfer, chat, pings and call setup wait behind one turn of file
  data at most (under a second at 57600 baud).

### Chat always shows your newest message
- **Fixed: the newest message stayed just below the screen.** After a
  swipe, Android keeps the list moving for a moment after your finger lifts.
  When it came to rest at the bottom, the chat still thought you were
  reading older messages and stopped following new ones.
- Being at the bottom now always means following new messages, however you
  got there.
- A message that changes height after it's shown (an emoji or a picture
  loading, a line wrapping) keeps the newest message in view.

### Calls
- A video call over the radio connects as a voice call, as intended: a
  radio carries a few KB/s, enough for voice but not video.

**Both phones need 0.13.6** (the radio link takes turns differently).

## v0.13.5 (2026-10-17)

### File transfers show their speed everywhere, including the radio
- **Fixed: a file sent over the serial radio showed no speed.** The speed
  was rounded to whole hundredths of a MB/s, so anything below about
  5 KB/s (radio speeds) showed as nothing. It is now shown in B/s, KB/s or
  MB/s as fits, with the **time left**: for example
  `1.2 MB / 147.6 MB · 0.8% · 4.6 KB/s · 8 h 52 min left`.
- The speed is measured over the last 8 seconds of real progress, and moving
  transfers are refreshed every second. It stays steady on a slow radio,
  where confirmations come seconds apart, and falls when a transfer stalls
  ("waiting…").
- **Chat no longer waits behind a file on the radio.** The radio link now
  has three lanes:
  - call audio first;
  - chat, confirmations, call setup and pings next;
  - file data last, sent in 2 KB pieces, so a message waits behind one
    piece at most (it used to wait behind up to 64 KB, about 20 s of radio
    time).

### Voice calls over the serial radio
- **Fixed: calls over the radio stayed on "Connecting…" for good.** Every
  voice packet went through the same reliable queue as chat and files, each
  one waiting for an acknowledgement, so the call never got through its
  setup.
- Voice packets now go as single unacknowledged radio packets, ahead of
  everything else: a late voice packet is useless, so nothing is resent.
  They stay sealed with the session keys, on top of the call's own
  encryption.
- Calls over the radio are tuned for it: Opus at about 8 kbit/s, mono, 60 ms
  packets (about 17 a second instead of 50), error correction, and nothing
  sent during silence. That's about 2.4 KB/s each way, which a radio pair at
  57600 baud carries. Tested in Chromium: the tuned call connects and sends
  60 ms packets at about 8 kbit/s.
- **Video over the radio becomes a voice call,** with a short note (a radio
  carries a few KB/s). Video works as before on Wi-Fi and mobile data.
- **No more endless "Connecting…".** A call that hasn't connected after
  45 s (90 s over the radio) ends and says why.

### Chat shows your newest message
- **Fixed: after sending, the newest messages stayed below the screen.**
  The chat scrolled to the bottom with an animation, and the positions
  reported halfway through read as "you scrolled up", so it stopped
  following new messages.
  - Jumps to the newest message are now instant.
  - Only your own scrolling (finger, mouse wheel, keys) stops the chat
    following new messages. The keyboard opening, a file bubble growing or
    a picture loading no longer does.
  - What you send always shows at the bottom.
- A background refresh of the chat no longer pulls you down to the bottom
  while you're reading older messages.

### Documentation
- README, the networking guide, SECURITY.md and the Android README cover
  the serial radio as it is now: private beacons, priority over the
  internet, Automatic adapter choice, voice calls, and speed with time left.
  Outdated lines are gone ("Serial/USB is disabled", "Calls over serial" as
  planned).

**Both phones need 0.13.5** for calls and file transfers over the radio
(the radio link has a new packet type for call audio).

## v0.13.4 (2026-10-17)

### Serial radios: both phones connect, and stay connected
- **Fixed: one phone showed SERIAL while the other never connected.** Three
  causes, all fixed:
  - **The two radios talked over each other.** Radio modems are half-duplex:
    when both send at the same moment, both transmissions are lost. That's
    the large "bad packets" count you saw in Settings → Serial. The link now
    takes turns: each side sends a short burst, then waits for the answer,
    and listens first so it never starts while the other side is sending.
    Packets are now 64 bytes (one USB packet, the smallest radio frame), so a
    lost frame costs less. The sending speed follows what the radio really
    delivers, and the wait for an answer follows the measured round trip,
    instead of resending too early. In a simulation of two half-duplex radios
    with small buffers, the old link never got the handshakes through; the
    new one does in 20-100 s depending on the radio's speed.
  - **The answering phone said "connected" too early.** It showed SERIAL as
    soon as it had *sent* its handshake answer. When that answer was lost on
    the air, the other phone never connected, and messages went into a
    session only one side had. Now the answering phone waits until the other
    side proves it has the session (its first encrypted message). Messages
    sent meanwhile are sent again then.
  - **An answer to an earlier try could break the session.** On a slow
    radio the dialing phone sometimes tries again before the first answer
    arrives. An answer now names the try it belongs to. The answer to *any*
    recent try completes the session, and a later answer brings both phones
    to the same keys.
  - **A background reconnect could cut a radio handshake short.** Once the
    phones had swapped their Wi-Fi addresses, the periodic "reconnect
    contacts" check tried those over TCP and threw away the radio handshake
    still on its way. A handshake on the radio is now left to finish.
  - When a session is dropped because the other side lost it, the chat
    header now shows that at once instead of staying on SERIAL.
- **The radio comes first.** If you're not on the same Wi-Fi/LAN as the
  contact, a working radio session is used instead of the internet.
  - An internet session (or Tor) is replaced by the radio as soon as the
    contact is in range. The radio session opens alongside, so nothing is
    lost while it connects.
  - While the radio works, an internet session can't take over.
  - The same Wi-Fi/LAN still beats the radio: when both phones are on it,
    they move there by themselves.
- **New radio picked up automatically.** *Settings → Serial → Serial port*
  has a new **Automatic** choice (now the default): it uses the adapter you
  plugged in most recently, and moves to a new one as soon as you plug it
  in.
  - A saved adapter is only used while it's plugged in; otherwise the one
    that is plugged in is used.
  - **To delete a saved adapter:** tap the trash icon next to the list (it
    shows when an adapter is saved). That switches back to Automatic.
- **Both phones need 0.13.4** (the radio packet format changed again).

### Release notes
- Tapping the version (bottom right) now shows these notes. It had been
  stuck on 0.11.0. The notes are now part of the app's source on GitHub
  too, and a test checks they match the version.

## v0.13.3 (2026-10-16)

### Serial radios: contacts only, and both phones agree
- **Fixed: one phone showed "connected over the radio" and the other didn't.**
  The cause: a slow radio carries one message at a time, so right after
  connecting (history sync, queued messages) the pings waited their turn.
  After ~30 s without a ping answer, one phone dropped the session as
  "dead". The other phone still had it, and nothing ever brought the first
  one back. Now:
  - a radio session is never dropped while packets keep arriving;
  - if one phone does lose its session (app restarted, a lost handshake
    answer), its next beacon tells the other one, and the two reconnect;
  - if the phone that should dial doesn't within 45 s, the other one dials;
  - a slow handshake is no longer restarted halfway through (which broke
    it every time on slow radios);
  - Tor can never take over a working radio session.
- **Nobody is discovered over the radio any more.** Only contacts you
  already trust can connect.
  - Phones no longer announce their name, ID or key on the air. They send
    private *beacons* that only the named contacts can read. To anyone else
    in range they're random bytes: they don't even show how many contacts
    you have.
  - A stranger's radio gets no answer, and nothing appears in your
    discovered list.
  - Under the hood: each pair of contacts shares a key only those two can
    compute (static X25519 from your identity keys). The session itself is
    the usual signed, quantum-safe handshake.
- **Both phones need 0.13.3** (the beacon replaces the old radio announce).

### Home Wi-Fi ↔ mobile data connects directly
- **Fixed: a phone on a home Wi-Fi and one on mobile data couldn't connect
  directly** (two phones on mobile data worked).
  - Many carriers use a new public port for every destination, and most
    home routers only let in replies from the exact port they sent to, so
    neither side's packets got through.
  - Now the phone on Wi-Fi asks its own router to forward the direct
    connection's UDP port (UPnP or NAT-PMP, as video-call and game apps
    do). The mobile phone's packets then get in from any port, and the
    first authenticated one fixes the path.
- **Safeguards:**
  - only routers on your own Wi-Fi are asked;
  - the port only answers your contacts' encrypted packets;
  - the mapping renews while the app runs and is removed when it stops.
- **New setting:** *Settings → Network → Open a port on my home router*
  (on by default).
- *Copy connection report* now says whether the router mapping is active.
  If the router has UPnP and NAT-PMP turned off, turn UPnP on in the
  router's settings.

## v0.13.2 (2026-10-16)

### Chat over serial radios
- **Fixed: phones found their radios but never connected.** The quantum-safe
  handshake is one ~9 KB message, and it was written to the radio in one
  burst. Radio modems hold only a small buffer and lose some bytes on the
  air, and with no resending a single lost byte broke the whole handshake.
- **New reliable radio link:**
  - every message goes in small packets (200 bytes) with a checksum;
  - the other side confirms what arrived and only missing packets are sent
    again;
  - the sending speed adapts to the radio (slower on loss, faster when
    clean), so its buffer never overflows.
  - Tested over a simulated radio with 3-5 % loss and corrupted packets:
    handshakes, chat and 10 KB messages arrive intact, in order and exactly
    once.
- **Same contacts over the radio:**
  - Serial now uses your normal identity, so the contact you paired over
    Wi-Fi is the same contact over the radio. (Before, the radio side was a
    separate "person" you had to add again.)
  - Phones in range announce themselves when the port opens (and every 30 s
    until connected), answer each other, and connect by themselves. The
    header shows **SERIAL**.
- **Live radio stats in Settings → Serial:**
  - whether you're connected and to whom;
  - bytes sent and received, good and bad packets, resends, when the other
    radio was last heard;
  - a plain explanation: bad packets mean a baud-rate mismatch; nothing
    received means the radios aren't paired (channel, net ID, air speed) or
    the other phone's serial is off.
- **Privacy:** announces over the radio no longer include your LAN IP
  address.
- **ESP32 boards:** opening the port no longer touches the RTS line, which
  on ESP32 can reset the board or put it in boot mode.
- The **Refresh** button is now an icon matching the rest of the app.
- **Both phones need 0.13.2** (the radio packet format is new).

## v0.13.1 (2026-10-16)

### USB serial adapters work on Android
- **Your serial adapter now shows up in Settings → Serial.** Android doesn't
  let apps open USB serial devices the way a PC does: they have to go
  through Android's USB system after you allow it. That's what the Serial
  USB Terminal app does, and now SRLTCP does too.
- Supported: FTDI, Silicon Labs CP210x, WCH CH340/CH341, Prolific PL2303,
  and boards with a standard USB serial interface (Arduino, ESP32,
  Raspberry Pi Pico, STM32, Teensy).
- **Permission:**
  - Android asks once to allow SRLTCP to use the adapter, either when you
    plug it in (accept SRLTCP) or when you pick it and press Save.
  - It then opens by itself.
  - If you say no, it doesn't ask again in the background; press Save to be
    asked again.
- **Plug and unplug:**
  - Pulling the adapter closes the port cleanly; plugging it back in reopens
    it within about 10 s.
  - Your choice is remembered across re-plugs (the adapter's vendor/product
    ID is saved, not a USB path that changes).
- Only real serial devices are listed: a keyboard, flash drive or USB-C
  headset is never offered or claimed.
- Settings → Serial has a **Refresh** button (the list also refreshes when
  you open the tab), Android-specific hints and the actual error if the
  port can't be opened.
- Only one app can use the adapter at a time: close the Serial USB Terminal
  app (or disconnect it there) first.

### Other fixes
- Changing the baud rate or port could leave serial closed for a few
  seconds (the old port's late close hit the new one). Fixed.
- A PC whose adapter is unplugged no longer logs read errors forever; the
  port reopens when the adapter is back.
- With no adapter connected, the message now says so plainly instead of a
  raw error code.
- The web UI had two copies of the "is this the Android app" check; the less
  complete one won. Fixed.
- Removed docs that still described the old relay.

## v0.13.0 (2026-10-16)

### Phone to phone only: relays removed
- **The internet relay is gone completely.** That includes the relay
  server, the relay code, the setup guide and installer, *Relay first on
  restricted networks*, and the external call-server (TURN) option. No
  server ever carries your messages, files or calls.
- Everything goes **directly between the phones**: on the same Wi-Fi or VPN,
  or straight across the internet over UDP (IPv4 through carrier NAT, and
  IPv6 at the same time, whichever connects).
- The only outside help is for *finding* each other:
  - **Tor** is the doorbell: the phones swap their addresses inside the
    encrypted session.
  - A **STUN** server tells each phone its own public IPv4 address. It
    learns your IP, nothing else.
- **Group chats** work the same way: every member over their own direct
  connection. Members who aren't each other's contacts get messages through
  the creator's phone, which is still phone to phone.

### Fixed: contacts on other networks sometimes never connected
- When the two phones swapped addresses before both had added each other as
  contacts, one side's addresses were dropped and never sent again. (The
  relay used to cover this up by accident.) Now each side says whether it
  already has the other's addresses and gets an answer if not.

### Connection report
- *Edit contact → Check connection → Copy connection report* shows:
  - whether this network's NAT is "easy" or "hard" (a new port for every
    destination);
  - whether IPv4 and IPv6 work;
  - what the contact offers;
  - how recent attempts ended.
- It contains no IP addresses. Please send it when two phones don't
  connect.

### Honest limits
- When **both** carriers use "hard" NAT, no direct technique can connect
  the phones. That's why other apps run relay servers. Messages then wait
  (never over Tor) and go out once one phone is on Wi-Fi or both have IPv6.
  Most US carriers offer IPv6 on mobile data; check *APN protocol:
  IPv4/IPv6*.

### Tested
- Simulated:
  - two carrier NATs with a private Tor network: found over Tor, delivered
    directly 11 s after the phones split, a 40 MB file at 7.4 MB/s;
  - two home routers: direct in 28 s;
  - a VPN phone and a home-Wi-Fi phone;
  - hard NAT on both sides: the message waits.
- **Both phones need 0.13.0.**

## v0.12.3 (2026-10-15)

### Your own relay in a few minutes
- **Settings → Network → Set up your own relay** shows a short guide and
  one command (with a copy button) to run on any small Linux server. It
  installs the relay as a locked-down service on port 443 (the standard web
  port), starts it after every reboot, and shows the relay code as text and
  as a QR code. Running it again updates the relay.
- **Scan relay code** next to the relay field: scan that QR code and the
  relay is set up. One phone is enough; your contacts get it inside the
  encrypted connection.
- The relay still can't read anything: it only passes on sealed, padded
  packets.

### Relay first on restricted networks (off unless you turn it on)
- **Settings → Network → Relay first on restricted networks.** On a Wi-Fi
  that keeps devices apart (plane, hotel, guest Wi-Fi), or when both phones
  are behind the same public address (Starlink, a carrier), a direct
  connection can't work. With this on, the app:
  - goes straight to your relay instead of first waiting for a direct
    attempt;
  - moves a contact it found over Tor onto the relay, so messages don't wait
    for a direct connection that won't come;
  - stops direct attempts there, which saves time and battery.
- Back to normal on the next network.
- It needs your relay and a network that allows ordinary internet traffic
  (on a plane: the internet plan, not the messaging-only tier).

## v0.12.2 (2026-10-15)

### Cleaner chats
- **No more connection notices in the chat** ("Found them over Tor",
  "Connected again directly", "disconnected"...). The connection state is
  shown in the header only.

### Wi-Fi with a sign-in page (planes, hotels, cafés)
- When the Wi-Fi is waiting for you to sign in, a bar at the top says so,
  with a **Sign in** button that opens the Wi-Fi settings (tap the network
  there to open its sign-in page). Android already knows this; the app sends
  nothing to find out.
- **Check connection** now also says what the network is doing:
  - the Wi-Fi keeps devices apart (common on plane, hotel and guest Wi-Fi),
    so two phones on it can't talk directly;
  - both phones share one public address (same Wi-Fi, Starlink or carrier),
    which most of these networks don't pass traffic through;
  - the network blocks UDP (what direct connections use).
- The app doesn't try to get around a network's sign-in page or its rules.
  On a plane your own relay is the way (if the plan allows it); otherwise
  messages wait and go out once you're on another network.

### File transfers reviewed
- **A file only appears under its name once it's verified.** Incoming
  bytes now go to a hidden partial file that the gallery and other apps
  ignore. The file gets its real name only after its SHA-256 check passes.
  A half-received or damaged file never shows up, can't be opened by
  mistake and isn't picked up by the gallery or backup apps.
- Two downloads with the same name can no longer collide.
- Also checked, no changes needed:
  - files are accepted only from trusted contacts, inside the encrypted
    session;
  - every chunk is encrypted and authenticated;
  - names and sizes are checked, and early data is kept within a limit;
  - compression is skipped for photos, videos and archives;
  - read, hash, compress and encrypt run off the main thread;
  - transfers resume from the last confirmed byte after a drop or restart.

## v0.12.1 (2026-10-15)

### Direct connections between networks: up to 20x faster
- **Fixed the slow file speed between a phone on mobile data and a phone on
  Wi-Fi (or two carriers).** The direct connection used a TCP-Reno-style
  speed control that treats every lost packet as a traffic jam. Radio links
  lose packets at random all the time, so at mobile round trips (150-200 ms)
  it settled at about 0.1 MB/s, exactly what you saw.
- It now uses **BBR**, the speed control YouTube and Google's QUIC use on
  mobile networks: it measures how fast data really arrives and paces
  packets at that rate, and random radio losses no longer slow it down.
- Measured on emulated mobile paths:

  | Path | Link speed | 0.12.0 | 0.12.1 |
  |---|---|---|---|
  | LTE, 140 ms, 1 % loss | 2.5 MB/s | 0.10 MB/s | 2.1 MB/s |
  | LTE, 90 ms, 0.2 % loss | 2.5 MB/s | 0.19 MB/s | 2.25 MB/s |
  | 5G far away, 200 ms | 7.5 MB/s | 0.11 MB/s | 5.2 MB/s |
  | Congested cell, 2 % loss | 0.62 MB/s | 0.10 MB/s | 0.56 MB/s |

### No more flipping between Tor and direct
- While the direct connection works, the contact can't replace it with a
  Tor session any more. That replacement was what made the chat show
  "Found them over Tor" and "Connected again directly" over and over, and
  restarted files each time.
- When one phone closes the direct connection, it now tells the other, so
  both look for a new path at once instead of one of them waiting 30 s on a
  dead link.

### Connects directly faster
- When the first attempt to connect directly fails (common with some home
  routers), the next attempt comes after 4 s instead of 10-40 s and uses a
  new port. Before, routers remembered the failed attempt for about 30 s and
  every quick retry kept failing. Two home routers in our simulation: direct
  after 25 s instead of almost 2 minutes.

### Messages during big files
- Chat messages, read receipts and file confirmations go out **ahead of**
  queued file data, and files travel in smaller pieces on the direct path,
  so a message no longer waits behind a video.
- Fixed: the phone *receiving* a file could hardly send anything back while
  the file came in (its own acknowledgements used up its sending budget).
  Replies and file confirmations now flow during transfers.
- Fewer acknowledgement packets (one per 4 data packets instead of 2): less
  work for the phone, about 20 % more speed when the phone's CPU is the
  limit.

- **Both devices need this version** for the full speed.

## v0.12.0 (2026-10-15)

### Tor is only the doorbell now
- **Your messages, files and calls never go over Tor any more.** Tor is
  used for one thing only: finding a contact on another network and swapping
  addresses so the two phones can connect **directly** (UDP or IPv6). Then
  everything runs at the full speed of both connections.
- While the direct connection is being set up the header shows
  **CONNECTING** (amber) and the chat says "Found them over Tor — connecting
  directly...". It usually takes a few seconds.
- Messages wait (tick: *Waiting to send*), files pause and calls ring once
  the direct connection is up — then they go out by themselves. Nothing is
  lost and nothing is sent slowly.
- **Faster setup:** a direct connection is started the moment the contact is
  found over Tor, either phone can ask the other to punch (no more waiting
  for the "leader" side), and retries come sooner (15 s, 30 s, 60 s, 120 s).
- The setting is now called *Find contacts on other networks (Tor)*.
- If both carriers use "symmetric" NAT (rare), a direct connection isn't
  possible: run your own relay, or messages wait until you're on a network
  that allows it. *Check connection* says which case you're in.
- Removed the 8 parallel Tor "lanes" for files (no longer needed), which
  also makes the app lighter.
- **Both devices need this version.**

## v0.11.0 (2026-10-15)

### Full speed between networks: direct UDP
- **Two phones on different mobile carriers (or mobile data and a home
  Wi-Fi) now connect straight to each other** instead of sending everything
  through Tor. They meet over Tor (or your relay), swap addresses inside the
  encrypted session, and punch a direct **UDP** connection through both
  carriers' NATs, the way video calls do. The header shows **DIRECT**.
- **Speed:** in our simulation of two carrier NATs (TCP between them blocked,
  packet loss) the phones were direct after about 10 seconds and a 40 MB file
  moved at 14-25 MB/s, about 100 times faster than Tor. Chat keeps flowing
  during big files, and call audio/video go as separate packets that never
  wait behind lost file data.
- **Private and secure:** every UDP packet is encrypted and authenticated
  with keys only you and the contact have (from the encrypted session), and
  the signed, quantum-safe handshake runs inside it. Packets from anyone else
  get no answer at all.
- Follows a phone to a new network (Wi-Fi to mobile data) after checking the
  new address really answers.
- When a network can't be punched ("symmetric" carrier NAT on both sides),
  Tor or your relay keeps carrying the traffic, as before.
- *Edit contact → Check connection → Speed* explains why a contact isn't
  direct and what to change.
- Uses a STUN server to learn your public address (Google's and Cloudflare's
  by default; set your own under *Settings → Network → STUN servers*). It
  learns your IP address, nothing else. Turning off *Direct internet
  connections* turns all of this off (Tor/relay only, contacts never see your
  IP).
- **Both devices need this version** for the UDP path (older versions keep
  using Tor).

### Discovery only on your own network
- **Nobody outside your own Wi-Fi/LAN or VPN can find you any more.**
  Nothing is announced on mobile data or over the internet, and
  announcements, *Find by IP* and contact requests from other networks are
  ignored. To add someone elsewhere, use a pairing code or link. Contacts you
  already have still reach you from anywhere.

### Security review
Three independent reviews attacked the new connection, the key exchange and
the whole app. Fixed:
- A contact could make the receiving side buffer unlimited out-of-order data
  (memory and CPU); now capped.
- A connection could be moved to an address that never proved it answers; now
  checked with a challenge first. A peer acknowledging data it never got is
  detected and disconnected.
- A UDP connection only accepts the contact it was made for.
- Stricter checks on addresses a contact may ask us to try (no tunnels that
  could wrap a LAN address, nothing on our own networks); hardware-derived
  IPv6 addresses are never shared.
- A contact can't pile up unlimited unfinished file offers (capped at 256);
  new contact requests are rate-limited.
- Everything else held: web UI only reachable from the phone itself,
  DNS-rebinding, CSRF, XSS (script policy), path traversal, Tor and relay
  access, forged or replayed packets.

## v0.10.10 (2026-10-15)

### Faster Tor when only one side knows the other's Tor address
- Files over Tor are split across several Tor circuits, opened by the
  sender, which needs the receiver's Tor address. When a contact had
  reached you over Tor before sharing theirs, the file crawled over a single
  circuit (about 0.1 MB/s). Now the sender asks, and the **receiver opens the
  circuits** instead; the sender uses circuits opened by either side.
  Tested: 8 circuits, about 2 MB/s on a private Tor network instead of one.

### Why isn't it direct? (Edit contact → Check connection)
- A new **Speed** line explains why a contact on another network is on Tor
  or the relay instead of a direct (full-speed) connection: whether each
  side has IPv6 (needed to connect directly between two mobile networks),
  whether direct tries were blocked, and how many Tor circuits are in use,
  with what to change: e.g. the phone's APN protocol set to IPv4/IPv6 (some
  carriers and MVNOs default to IPv4 only), your own relay, or a VPN such as
  WireGuard or Tailscale on both phones.

## v0.10.9 (2026-10-15)

### New logo
- **"Cipher rings":** neon green rings like a cipher wheel around a core, on
  black. Used for the app icon (it fills round, squircle and rounded-square
  shapes), the themed icon on Android 13+, the status-bar and notification
  icon, and the browser tab on a PC.

## v0.10.8 (2026-10-15)

### Fixes
- **App icon centered.** The logo sat a little to the right and up; it's now
  in the middle of the icon (also the themed and notification icons).

## v0.10.7 (2026-10-15)

### Fixes
- **Call buttons fit small phones.** With the camera-switch button there are
  five buttons in a video call, and on phones like the Pixel 5 the outer ones
  were cut off. They now size themselves to the screen (64 px on larger
  screens, smaller on narrow ones) and always fit.
- **App icon:** the logo on black now fills the icon shape. Android showed it
  as a small black square inside a white circle because the app used a
  plain (non-adaptive) icon. On Android 13+ the icon also follows the
  phone's themed-icons setting.
- **Notification icon:** the status bar now shows the logo outline instead of
  a white square.

## v0.10.6 (2026-10-15)

### Calls
- **"Answer" on the ringing notification answers the call.** Before, it only
  opened the app and you had to answer again inside it. A video call is
  answered with video. (Tapping the notification itself still just opens the
  call screen, where you can choose voice or video.)
- **Switch between the front and rear camera** during a video call with the
  new camera button. The other side keeps seeing you without the call
  restarting. Your own preview is mirrored only for the front camera. The
  button shows only on devices with more than one camera.

## v0.10.5 (2026-10-15)

### Calls survive a network switch
- **Switching networks no longer ends a call** (Wi-Fi to mobile data, leaving
  home, the VPN reconnecting). The call shows "Reconnecting…" and carries on
  by itself as soon as the two devices are connected again, with voice and
  video. It waits up to 90 seconds before giving up. Tested by freezing one
  side for 35 seconds in the middle of a video call.
- How soon it comes back depends on how the devices find each other on the
  new network (direct, Tor or your relay). A PC is reachable from mobile data
  only with Tor installed on it (Tor Browser) or a relay.

## v0.10.4 (2026-10-15)

### Video calls with a PC whose camera won't open
- **The call goes ahead with voice** when the camera can't be opened (busy in
  another app, blocked by Windows or the browser), instead of ending at once.
  You still see the other person's video.
- **It says why**, and what to change: the browser's permission for the page,
  Windows Settings → Privacy & security → Camera / Microphone ("Let desktop
  apps access…": browsers count as desktop apps), another app using the
  camera, or the laptop's camera privacy switch.
- The camera is tried again without a preferred picture size, which some
  Windows webcam drivers refuse.
- If the other device can't open its microphone at all, your phone now says
  so instead of just "Call ended".

## v0.10.3 (2026-10-15)

### QR scanner that focuses
- **The camera keeps focusing** while you scan, instead of refocusing only
  every couple of seconds.
- **Tap to focus** on the code.
- **Starts zoomed in (about 2×)**, so you can hold the phone farther back:
  phone cameras can't focus closer than about 10 cm. Pinch or use the slider
  to zoom, and the **Light** button turns on the flashlight in the dark.
- Reads codes shown on a dark (dark-mode) screen too, and tries harder on
  blurry or small codes.

## v0.10.2 (2026-10-15)

### Tor files no longer freeze for 40 seconds
- **A stuck Tor circuit no longer stops the whole file.** Files over Tor are
  split across several circuits; when one of them stalled, the file waited
  about 40 seconds and then sent everything in flight again, which is why a
  Tor transfer could crawl at ~0.2 MB/s. Now, after 6 seconds without
  progress, only the stuck circuit's part is sent again on the other
  circuits and that circuit is replaced.
- **Mixed versions:** a phone on 0.10.0 accepts fewer Tor circuits than
  0.10.1 opens; the refused ones made the sender start over again and again.
  The sender now uses only as many as the other phone accepts (and this
  version accepts up to 16). Update both phones for the best speed.
- **See the circuits:** a file moving over Tor shows "Tor ×N" (how many
  circuits carry it) next to its speed.

### Fixed
- The first 0.10.2 download still contained the 0.10.1 app (it showed
  v0.10.1 at the bottom of the screen and had none of the fixes above).
  Install the new download over it; your chats are kept. The build now
  always includes the current app code and stops if the versions don't match.

## v0.10.1 (2026-10-15)

### Faster files when you start on Tor
- **A direct path is looked for the moment a file starts over Tor** (same
  Wi-Fi, VPN or IPv6), and every 15 seconds while it's moving, instead of
  every 45 seconds. When one works, the file moves over mid-transfer and
  carries on from where it was (in testing: from 0.18 MB/s on Tor to over
  9 MB/s direct). Earlier failed hole-punching attempts no longer hold the
  next try back.
- **Twice as many Tor circuits for file data** (8 instead of 4), with twice
  as much data in flight. Tor limits each circuit to roughly 0.3 MB/s, so a
  file over Tor gets up to about twice as fast as before; it still can't
  match a direct connection.

## v0.10.0 (2026-10-15)

### Folder sync
- **Keep a folder the same on your device and a contact's.** Contact's ⋮ →
  *Sync a folder…*; they accept and choose where their copy goes. Adding,
  changing or deleting a file on either side is copied to the other whenever
  you're both connected (Wi-Fi, VPN, IPv6, Tor or relay), end-to-end
  encrypted, only what changed.
- If you both change the same file before it syncs, both versions are kept
  (one as a "conflict" copy). Deleted files go to a hidden trash folder on
  the other side for 30 days.
- Both devices need this version.

### Faster
- The CPU/temperature bar no longer stalls the app: reading the CPU load
  paused the whole node for a third of a second every second while the app
  was open (chat, calls and transfers all waited).

## v0.9.0 (2026-10-15)

Both devices need 0.9.0: the handshake changed (older versions can't connect
to it and say so in the log).

### Quantum-safe encryption
- Every session now combines **X25519 with ML-KEM-768** (the NIST
  post-quantum standard) for the session key, and every handshake is signed
  with **Ed25519 and ML-DSA-65**. Recorded traffic stays private even against
  a future quantum computer; breaking one algorithm alone isn't enough.
  Speed is unchanged after the first second of a connection.
- Each contact's post-quantum key is remembered the first time you connect;
  a different key later is refused.

### Faster paths
- **Direct over IPv6, straight from Tor.** When both devices have IPv6
  internet, they swap addresses inside the Tor session and connect directly
  (no relay, no server): the badge goes from TOR to DIRECT and files move at
  the full speed of both connections. If a network blocks it, Tor carries on.

### Verified contacts
- A shield next to each contact's name shows whether you checked their keys.
  **Pairing in person (QR scan or NFC tap) verifies both phones by itself.**
  For anyone else, tap the shield: scan the code on their phone, or compare
  the 60-digit safety number and tap *The numbers match*.

### Privacy (Settings → Privacy)
- **Encrypted on the device:** your keys, contacts, chats, groups and
  settings are stored encrypted; on Android the key is held by the phone's
  secure hardware (Keystore). Existing data is encrypted on first start.
- **Encrypted backup and restore:** one file locked with your password, with
  your identity, contacts, chats, groups and settings. Restore it on a new
  phone (also offered on the first-run screen) and your contacts see the same
  you.
- **Disappearing messages** (off by default): what you send is deleted from
  both phones after 1 hour, 1 day, 1 week or 4 weeks.
- **App lock** (Android, off by default): fingerprint, face or screen lock to
  open SRLTCP, and again after 30 seconds away.
- **Hide message content on the lock screen** (Android, off by default).

### Files
- **Transfers survive restarts:** close the app (or restart the phone) in the
  middle of a file and it carries on from where it was once both of you are
  back. A file that was edited in the meantime isn't resumed.
- **Send several files at once** with the paperclip.
- **Shrink photos before sending** (Settings → General, off by default):
  smaller and faster to send, and without the photo's location data.

## v0.8.3 (2026-10-14)

- **No more connect/disconnect alerts.** A contact connecting or dropping
  off no longer pops up a notification or a message at the bottom of the
  screen. You see it only in that contact's chat, while it's open: in the
  header (Online, path, ping) and as a line in the chat. A quick drop and
  reconnect (switching path) leaves no lines behind.
- **Simpler contact screens:** the "Internet address (advanced)" section is
  gone from *Edit contact* and *Add contact* (Tor and the relay reach
  contacts over the internet by themselves). Addresses you saved there
  before are kept.

## v0.8.2 (2026-10-13)

### Calls ring like phone calls
- An incoming voice or video call rings with **your phone's ringtone**,
  repeating until you answer or decline, or the caller hangs up (45 s at most).
  It follows your phone's ring/vibrate/silent setting.
- It shows as a call (over the lock screen, with **Answer** and **Decline**),
  not as a message notification. Decline works without opening the app.
- A call that arrives while SRLTCP is closed is still there when you open
  the app from the ringing notification.
- The app only shows over the lock screen while a call rings or runs, never
  your chats.

### Files
- **Fixed: the sender's bubble showed "0 B / 14.3 KB · 0%"** for a small file
  that had arrived. A progress update scheduled a moment earlier overwrote
  "complete".
- **Two checkmarks for files:** once the other device has the whole file
  and has checked it, the file bubble shows ✓✓, like a delivered message.

## v0.8.1 (2026-10-13)

- **"Upload failed" now says why.** When a file can't be sent, the message
  names the reason: the phone couldn't read the file (for example it was
  still downloading, or was moved), storage is full, or the file stopped
  arriving part-way. The same reason is written to *Settings → Advanced →
  Activity*.
- The app recreates its upload folder if it's missing, instead of failing.
- Installs over 0.8.0 (same signing key); contacts and chats are kept.
- Android: one APK for phones (arm64).

## v0.8.0 (2026-10-13)

**Update both devices**: 0.8 uses a new handshake version (5) and doesn't
talk to 0.7.x.

### Fixed: cancelling a file didn't stop it on the other device
- Cancelling on the sending side left the receiving device showing the file
  as still transferring, and it kept writing data. Cancel now works from
  either side, shows on both, and the half-received file is deleted. If the
  cancel is lost (connection dropping at that moment), the other device is
  told again as soon as it sends anything for that file, or on reconnect.

### Fixed: large files could arrive damaged
- Pieces of a file were written by several tasks at once, and a piece could
  cut off the one written just before it. Big files then failed the final
  check ("Transfer failed"); in our test a 512 MB file failed every time.
  Files are now written strictly in order through one open file, and checked
  while they're written.

### Faster file transfers
- **Over the LAN / VPN**: the sender no longer waits for each piece to leave
  before preparing the next, and the receiver no longer opens and closes the
  file for every piece. Reading, checking, compressing and encrypting run
  next to the network instead of in turn. No more extra pass over the whole
  file before sending or after receiving. 512 MB between two processes:
  ~90 MB/s, verified (it used to end damaged).
- **Over Tor**: one Tor circuit tops out at roughly 0.5 MB/s. A file is now
  spread over 4 extra Tor circuits at once (each separately routed, opened
  only for file data and closed a minute after the last file). Messages,
  calls and acknowledgements stay on the main circuit, so chat doesn't stall
  behind a file. Lanes are joined with a proof from the session key, so
  only your contact can attach one.
- **Relay or Tor -> same Wi-Fi**: a session on the relay or Tor now moves to
  the direct path even while a file is moving (it carries on from where it
  was), and right away when the contact shows up on your network instead of
  up to 45 s later.
- Already-compressed files (photos, videos, zips, …) aren't compressed again,
  saving CPU and battery on phones.

### Clearer progress
- The progress, live speed (measured over the last moments, not averaged
  since the start) and a cancel button are in the file's own bubble, in the
  chat with the contact it's going to or coming from. The panel above the
  message box that showed every transfer in whichever chat was open (a file
  to Bob appeared in Carol's chat) is gone, as is the duplicate list in
  Settings.
- The sender's progress shows what the other device has actually saved, and
  "complete" only once it has verified the file.

### Reliability
- Lost pieces (a path change, a re-key, a dropped connection) are resent
  from the last piece the receiver confirmed; a transfer that gets no answer
  at all reconnects instead of hanging.
- A device low on storage refuses a file it can't hold instead of filling up.
- Chat messages no longer wait behind a file's data on the same connection.

### Privacy and cleanup
- Copies of files sent from the browser (kept in the app's uploads folder)
  and temporary folder zips are deleted at startup; they were never cleaned
  up before.
- Removed the retired Noise XX handshake code (unreachable since 0.6.1) and
  two unused dependencies (`noiseprotocol`, `aiofiles`), plus other dead code.

### Testing
- New tests: out-of-order pieces, a damaged file is refused and deleted,
  cancel from either side, a lost piece is resent, a re-key mid-transfer, a
  file moving from the relay to the direct path mid-transfer, Tor lane
  joining (forged joins refused), memory bounded against a flooding sender.
- Private Tor network test: an 8 MB file over 4 parallel circuits, chat still
  flowing. Browser simulation: cancel from the bubble shows on both sides.
  167 tests pass; app pentest 66/0.

## v0.7.2 (2026-10-13)

### Fixed: sending files failed
- **Files to a contact who isn't reachable right now** (other network, Tor
  still connecting, phone asleep) used to fail with "Cannot send file" or
  "File send failed". They now wait, like messages: the chat shows *waiting
  for connection*, and the file goes out by itself as soon as the contact is
  reachable again.
- **A file offer lost when the connection dropped** left the transfer stuck
  as "offered". Because that counted as a transfer in progress, the app also
  stopped checking that contact's connection, so later files and messages
  went into a dead connection. Now an unanswered offer is sent once more,
  then waits for the next connection, and the connection check keeps running.
- The chat header no longer says "Online" after a contact disconnects.

### Fixed: loudspeaker not working in calls on some phones
- The earpiece/speaker choice was applied once, when the call connected. On
  some phones the WebView's audio engine reset it a moment later, when the
  other side's audio started, so the speaker button did nothing. The choice
  is now applied again at that point, shortly after, and whenever a headset
  is plugged in or removed.
- On Android 12+, a phone that refuses the newer audio-routing call now falls
  back to the older speakerphone switch instead of failing silently.
- The speaker button also works on older WebViews that never report the
  call as connected.
- An "Add them back?" prompt no longer stays on screen after that contact
  was added another way (for example by scanning a QR code).
- File names with two dots in a row (e.g. `report..v2.pdf`) could be uploaded
  but then not sent. Fixed. If the device's storage is full, the error now
  says so.

### Testing
- New tests: a file sent to an offline contact arrives when they're back, in
  the browser simulation too; an unanswered offer is resent; a 3 MB file over
  Tor. 161 tests pass. App pentest 66/0, relay pentest 44/0.

## v0.6.0 – v0.7.1 (summary)
- **Tor** (0.7.x): reach trusted contacts from any network with no server or
  port forwarding; only mutually trusted contacts can connect.
- **Direct connections across networks** (0.6.x): address exchange, parallel
  dialing, hole punching, IPv6; a VPN phone and a Wi-Fi phone talk directly.
- Shared folders rebuilt (files or folders, contacts or groups, password,
  time limit); QR pairing adds both sides; deleted contacts' chats kept 24 h.
- Disconnect notices in the chat and *Check connection* explain why a contact
  can't be reached.

## v0.5.0 (2026-10-12)

**Update both phones** (and replace any old hub with the new relay): 0.5 uses a
new handshake and doesn't talk to 0.4.x.

### Talk over the internet, automatically
- Pair once on the same Wi-Fi or VPN; after that the phones keep talking with
  the VPN off, on different carriers, on Wi-Fi or mobile data
- They use a **direct** path whenever one works and your own **internet relay**
  otherwise, switching by themselves: the relay takes over within seconds when
  the VPN goes off (3 s in our test) and they move back to direct when it
  returns (18 s). Messages, files, voice and video calls all work over the relay
- Set up: run `srltcp relay --public-host YOUR.SERVER.IP` on your server (the
  WireGuard server is ideal), paste the code it prints into *Settings → Network
  → Internet relay* on **one** phone. The other phone gets it automatically.
  Guide: docs/INTERNET_RELAY.md (includes a sandboxed systemd service)
- Contacts show **TCP** (direct) or **RELAY**

### Private by design — the relay is treated as untrusted
- TLS 1.3 to the relay with its certificate pinned (no impersonation) and an
  access key (strangers can't use it)
- No names, identities or keys reach the relay: phones listen on mailboxes
  derived from a secret only the two contacts share, rotating daily
- Every relayed packet is sealed again with that pair key, padded to fixed sizes
  and replay-checked: the relay can't read message types, inject or replay
  packets, and stores nothing
- Replaces the old hub, which saw identity hashes and names

### Security
- Handshake v4: the signature names the intended recipient, so a captured
  handshake can't be replayed to another device (fixes the known limitation)
- Penetration tested: `scripts/pentest/pentest_local.py` (web API, peer port,
  file transfers, discovery, pairing, call relay) and
  `scripts/pentest/pentest_relay.py` (relay server, clients, rendezvous,
  privacy). Findings and fixes: docs/SECURITY_AUDIT.md
- Fixed: discovery announce floods could grow memory without limit (now
  bounded); frames limited to 4 MiB (was 64 MiB) so unauthenticated
  connections can't make the app buffer much; malformed requests get 400
- Contact secrets are stored owner-only and never shown to the web UI


## v0.4.1 (2026-10-12)

- **Swipe to stop.** Swiping the "SRLTCP — Running" notification away now stops
  the app completely (no more App info → Force stop). This works on Android 14
  and newer; older Android versions don't allow swiping that notification, so
  use its **Quit** button there. Opening SRLTCP starts it again
- Quit also closes the app screen if it is open


## v0.4.0 (2026-10-12)

Update every phone (and your hub, if you run one).

### New: group chats
- *Groups → New group*: name it, tick contacts, done. Everyone you add gets it
  automatically
- End-to-end encrypted over the normal one-to-one connections, no group server.
  Each message is signed by its author; the group's creator passes messages on
  to members who aren't each other's contacts, and can't fake anyone's message
- Missed messages and member changes arrive when someone comes back online.
  Rename / add / remove members (creator), leave, close. Text only for now

### Calls
- **You can hear and see each other.** Calls connected but Android blocked the
  incoming sound and picture (the big play button). Fixed, and call audio now
  goes to the earpiece (voice) or speaker (video) like a phone call, with a
  speaker button; the screen stays on during calls
- Voice calls show a phone icon and "Voice call"; video calls a camera icon and
  "Video call" — on both phones. A voice call can't be answered with video
- The microphone always starts on; mute (and camera off) turn red
- Calls connect more reliably (early connection details were sometimes dropped)

### Fixes
- **The send arrow works** on phones (before, only the keyboard's send key did)
- **Chats open at the newest message**, also when coming back from the background
- **No more pinch-zoom on the app**; it fits the screen. Pictures zoom in the
  viewer with two fingers or a double-tap
- Messages written with the VPN off are sent automatically when it is back
  (tap a message's tick to see its status)
- A file transfer cut off by a lost connection now resumes by itself (it used to
  stall and also stopped that contact from reconnecting)
- Notifications for the open chat while the app is in the background; no more
  duplicate alerts for files
- Switching chats never shows the previous chat's messages; long histories load
  per chat; messages no longer vanish when a slow refresh lands late
- Back gesture on a contact request closes it instead of declining
- Android: NFC pairing restarts after the QR scanner or settings, answers only
  while the app is open, and doesn't pair twice; dark-mode switches no longer
  restart the app mid-call; "Save as" survives the app being recreated; the
  all-files-access screen is only shown once; crashes are no longer swallowed

### Security
- A stranger flooding the TCP port with junk could freeze the app; parsing is now
  linear, unauthenticated connections close after 30 s, connections are capped
- Hub: a copied registration could hijack another member's routing; hub
  registrations are now time-stamped and must be newer (update hub and phones)
- A replayed announce could redirect a contact's saved address; only newer
  announces may change it
- The Android app no longer trusts user-installed certificates for its local page,
  and the JavaScript bridge only works for the app's own page
- Back-pressure on incoming files, caps on pending contact requests, temporary
  share zips are deleted after sending


## v0.3.1 (2026-10-12)

Update both phones — calls need 0.3.1 on each side.

### Fixes
- **Calls and video calls connect.** They used to ring and then hang on
  "connecting" because the phones' browsers could not find a media path between
  them. Call audio/video now travels inside the encrypted SRLTCP connection
  through a small relay built into each device, so calls work everywhere chat
  works: same Wi-Fi, WireGuard, or via the hub across carriers. No TURN server needed
- **Pairing works both ways.** Adding someone now pops up *Contact request —
  Add them back?* on their phone (also when they were offline at the time, and for
  expired pairing codes). Accept and you are contacts on both sides
- **Back gesture no longer closes the app.** On the Pair screen (and other
  popups, settings, a chat) it goes back to the main screen; on the main screen
  it sends the app to the background instead of quitting
- **Downloading files works.** *Download file* asks where to save: Android
  opens the system *Save as* screen, desktop Chrome/Edge show a save dialog
- **Opens on Trusted** when you have contacts (Discovered when you have none)


## v0.3.0 (2026-10-12)

Works with 0.2.x devices. Update both phones to get every fix.

### Works over WireGuard / VPN
- **Announce now finds phones on a WireGuard VPN.** VPNs carry no broadcast
  packets, so 0.2 could never discover anyone over one. Announce now also sends
  its signed announce directly to every address in the VPN subnet, and devices
  answer — one press on one phone and both appear under *Discovered*
- **Find a device by IP** in *Add Contact* (e.g. `10.0.100.5`)
- **Android sees the VPN address** — interfaces are now read through Android's
  own API (the app could not see `tun0` before and advertised the Wi-Fi/mobile IP)
- **Addresses fix themselves** — when a phone's IP changes it tells its contacts
  immediately; contacts update and reconnect (14 s in our WireGuard test, even
  with discovery off). Pairing codes carry every address, VPN first
- Tested on a real WireGuard network (wireguard-go): discovery, trust, messages,
  files, video calls and IP changes — `scripts/simulate_wireguard.sh`

### New
- **Edit contact** — rename, change address/port, internet address, mode, block,
  delete; from the ⋮ button, a long-press, or tapping the name in a chat
- **NFC that just works** — open *Pair* on both phones and hold them together;
  no button to press. Vibration on success; prompt when NFC is off
- **Runs in the background** — messages, calls and contact requests keep
  arriving after you close the app (notification with a *Quit* button)
- **Call relay (TURN)** setting for calls across mobile networks without a VPN

### Fixes
- Dead connections (network switch, phone asleep) are detected within about a
  minute and replaced, instead of messages disappearing into them
- Contacts come back online by themselves (background redial)
- Blocked contacts can be unblocked again (they used to vanish from the list)
- Enabling the hub no longer hides LAN/VPN peers; direct addresses are preferred
- Network interface defaults to *Automatic*; on upgrade the Android app unpins the
  address saved by 0.2's setup and turns discovery on
- Connecting to an unreachable address times out after 8 s (was ~2 minutes)
- Opening an offline contact says "messages will be delivered" instead of an error
- Find-by-IP / Edit contact layouts on phones; Android service restarts the node
  itself; activity no longer reloads the page on every resume


## v0.2.0 (2026-10-12)

> **Not wire-compatible with 0.1.x.** Update every device; old and new
> versions will not link.

### New
- **Pairing** — Pair button: show a QR code / shareable link, or paste/scan
  someone else's. One scan pairs both sides (signed contact card + one-time
  token). On Android: **tap phones together (NFC)** or use the camera scanner
- **Voice & video calls** — WebRTC, end-to-end encrypted; call setup travels
  inside the existing encrypted session. No STUN/TURN by default (LAN/direct);
  optional STUN server in Settings → Network for internet calls
- **Delivery receipts** — ✓ sent, ✓✓ delivered, ⚠ waiting for them to accept you
- **Offline messages** — queued, retried in the background and delivered when
  the contact comes back; sending never blocks
- **Chat history saved to disk** (follows the "Keep messages" setting)
- **Contact requests** — someone who isn't your contact yet shows up in
  Discovered as "wants to chat" with an Accept button

### Security fixes
- Handshake now proves the peer owns the key behind its hash ID (contacts
  could previously be impersonated), with replay protection and pinned keys
- Noise XX mode derived its keys from public data — fixed, and identities are
  now bound to the Noise session
- Encrypted messages use counters + a replay window; header bound as AAD;
  unencrypted application messages are rejected
- Messages from untrusted peers are no longer shown as chat messages
- A connected peer could overwrite a file you were sending, request your
  outgoing files, or cancel others' transfers — fixed
- Shared folders: requester identity comes from the session (was spoofable);
  stricter path checks; symlinks leaving the share are skipped
- Hub: authenticated sender on relayed packets; signed presence
- Local web UI/API now requires a per-install token (other apps on the phone
  could read messages and delete folders via 127.0.0.1)
- Android: localhost certificate pinned, external links open outside the app,
  camera/mic only for the local UI
- Removed Google Fonts (third-party request on every launch)

### Bug fixes
- Link-up was never processed (UI not told, paused transfers never resumed)
- Offline message queue was never sent
- Simultaneous dials produced mismatched keys
- Android LAN discovery: Wi-Fi multicast lock (phones dropped broadcasts)
- Android build failed on JDKs other than 17 / without
  `/usr/lib/jvm/java-17-openjdk`
- Hub dropped a live member when its old connection closed; clients now
  register on connect
- Contact list rows broke on "Trust" (nested buttons); phone chat header hid
  the contact name; spurious "restart with --port" toast
- `srltcp send` waits for the delivery receipt; port 0 binding reports the
  real port

### Testing
- End-to-end tests with real nodes over loopback (direct, hub, Noise, pairing,
  offline, impersonation, malicious peers) and `scripts/simulate.py`, which
  drives three nodes' UIs in Chromium (pairing, chat, files, video call,
  phone layout, discovery, offline delivery)

## v0.1.57 (2026-07-06)

### Security
- **Signed LAN announces** — UDP discovery (7826) announces are Ed25519-signed; receivers verify signature + identity binding before trusting a peer; unsigned/spoofed announces are rejected
- **Noise XX handshake (experimental)** — optional `Noise_XX_25519_ChaChaPoly_SHA256` in Settings → Network; both peers must use the same protocol

### Docs
- README and SECURITY.md updated for signed discovery and Noise handshake option

## v0.1.56 (2026-07-06)

### Android
- **Stack navigation** — sidebar is the home screen; chat is a full-screen layer; settings only from sidebar
- **Back gesture** — returns from settings or chat to the sidebar (peer stays selected)
- **Media** — images/videos open fullscreen without raising the keyboard
- **Send** — keyboard stays open after tapping Send

### Docs
- README: removed data flow diagram and wire protocol sections

## v0.1.55 (2026-07-06)

### Hub
- **LAN hub dial** — optional **Hub LAN address** (tried before public host); private IPs allowed for same-network clients
- **Announce crash fix** — hub unreachable returns a clear 503 error instead of HTTP 500 / `RuntimeError`

### Android
- **Resume last chat** — `localStorage` restores the open peer after minimize; cold start still opens sidebar first
- **Removed** edge-swipe sidebar/minimize gestures from v0.1.54

## v0.1.54 (2026-07-06)

### Android
- **Background notifications** — native alerts for messages, transfers, and disconnects while the app is backgrounded (foreground service + alert channel)
- **Session restore** — reopening from recents returns to the same chat; cold start still opens the sidebar first
- **Edge swipe** — swipe from the left edge in an active chat opens the sidebar; swipe again minimizes to background
- **Settings UI** — folder row layout tweaks; hide Delete on narrow/mobile folder rows

## v0.1.53 (2026-07-05)

### Android
- **APK naming** — output is `SRLTCP-0.1.53.apk` (no `-debug` suffix)
- **Chat layout** — single status-bar inset (fixes excess padding from v0.1.52); composer sits just above the footer bar
- **Swipe to close** — suppress WebView “connection refused” error page when removing the app from recents
- **Load retry** — fixed stacked retries hitting wrong ports (9877/9878) after backgrounding

## v0.1.52 (2026-07-05)

### Android
- **Folder delete** — Settings → Folders **Delete** removes the folder on device and clears the saved path (no longer re-applies Downloads default after you clear it)
- **File attachments** — WebView file picker (`WebChromeClient.onShowFileChooser`) so paperclip send works on Android
- **Status bar** — extra bottom padding for chat (tuned in v0.1.53); CPU stat hidden on Android (temperature only)

### Docs
- README changelog moved here; development and Android build instructions expanded

## v0.1.51 (2026-07-05)

### Android
- **Downloads defaults** — incoming and shared folders default to `Downloads/SRLTCP/Incoming` and `Downloads/SRLTCP/Shared` on first launch (visible in Settings → Folders)
- **Folder picker** — browse starts in Downloads on Android when storage access is granted
- **Mobile UI** — slide-out sidebar (☰), full-screen settings (⚙), phone layout, chat above status bar
- **Startup fixes** — server health check, port fallback, storage permissions, WebView retry

### Project
- **Local builds only** — removed GitHub Actions checks workflow; APK and releases built on your machine
- **Version** — 0.1.51 (versionCode 51)

## v0.1.50 (2026-07-05)

### Hub connectivity (replaces legacy relay)
- **Headless hub** — `srltcp hub --bind 0.0.0.0 --port 7825` for a shared meeting point on the internet
- **Hub clients** — Settings → Network: enable hub, set host/port; click **Announce** to register and discover other hub users
- **E2EE tunneling** — `RELAY_ENVELOPE` opaque forwarding; hub sees routing tokens only, not message or file content
- **Signed registration** — hub presence requires Ed25519-signed `HUB_REGISTER` payloads
- **Removed** — `srltcp relay` (port 7827), `web --relay`, multi-hop `RoutingTable` / `ROUTE_UPDATE`

### Android (Gradle + Chaquopy rebuild)
- **Removed** — Buildozer, python-for-android, `build-android.yml` GitHub workflow, `p4a-recipes/`
- **Added** — standard Gradle project with Chaquopy 15, `./gradlew`, committed wrapper
- **Scripts** — `scripts/sync-android-python.sh`, `scripts/build-android.sh` for local APK builds
- **Docs** — `android/README.md` with JDK 17 + SDK prerequisites

### CI & documentation
- **Checks workflow** restored — `ruff`, `mypy`, `pytest` on push/PR (no cloud APK build)
- **SECURITY.md** — hub operator trust, connection modes, Android hardening
- **README** — hub usage, local Android build, updated settings table

## v0.1.48 (2026-07-03)

### File transfers (receiver)
- **Auto-finalize** — incoming transfers mark `complete` when all bytes are on disk, even if `FILE_COMPLETE` is lost on link drop
- **Disconnect recovery** — finalize or fail stuck incoming transfers when the peer disconnects (unblocks preview/UI)
- **File send** — uses active link transport (TCP vs serial) instead of discovery default

## v0.1.47 (2026-07-03)

### Android CI
- **p4a python3 override** — `get_recipe_dir()` points at upstream patch files (fixes missing `reproducible-buildinfo.diff` in local recipe)

## v0.1.46 (2026-07-03)

### Media previews (receiver)
- **Transfer state merge** — chat preview uses terminal `complete` from message metadata even when stale WS progress is cached (fixes receiver screenshots stuck as paperclip)
- **Filename fallback** — infer image/video type from `.png`/`.mp4` extension when `msg_type` is generic `file`
- **File API** — prefer `_incoming_paths` for received files when serving inline preview

## v0.1.45 (2026-07-03)

### Android CI
- **grp module** — local p4a recipe disables `grp` via `Modules/Setup.local` (fixes `setgrent` compile failure on arm64-v8a with Python 3.12.8)
- **serial_access** — tolerates missing `grp` on Android

## v0.1.44 (2026-07-03)

### Android CI
- **SDK/NDK paths** — removed invalid `%(ENV_ANDROIDSDK)s` from spec; CI symlinks runner SDK + NDK into `~/.buildozer/android/platform/`
- **platform-tools** — installed so buildozer's `adb` check passes

## v0.1.43 (2026-07-03)

### Android CI
- **buildozer.spec fix** — moved all `android.*` keys into `[app]` (buildozer ignores `[android]`; caused default dual-arch `arm64-v8a` + `armeabi-v7a`)
- **Removed `buildozer android clean`** before first build — p4a is not cloned yet at clean time in CI

## v0.1.42 (2026-07-03)

### Media & transfers
- **Image preview** — chat shows image/video thumbnails only when transfer state is `complete` (fixes receiver missing preview and serial broken thumbnails)
- **Transfer dock removed** — bottom progress bar removed; per-message transfer progress remains

### Serial
- **Link quality** — uses smoothed RTT (not per-ping spikes) plus EMA on displayed %; label is still an RTT/error estimate, not RF RSSI

### UI
- **Add contact** — optional WAN host, port, enable toggle, connection mode (auto/LAN/WAN)
- **Folders modal** — scrollable file list; compact path breadcrumb
- **Restart button** — higher contrast in Advanced settings

### Android CI
- **Single arch** — `buildozer android clean` then debug build from `android.archs = arm64-v8a` only (drops CLI `--arch` that caused dual-arch `grp` failures)

### Docs
- README security section — data paths, WAN notes, what is / is not encrypted

## v0.1.39 (2026-07-03)

### Launcher
- **`./run.sh stop`** — kills stale SRLTCP and frees ports 7825/7826/9876
- **`./run.sh web`** — auto-stops any previous instance before starting
- **Python 3.14** — UDP bind fallback when `reuse_address` is unsupported

## v0.1.38 (2026-07-03)

### Serial discovery
- **Arch permissions** — error hints use `uucp` group (Arch) or `dialout` (Debian/Ubuntu)
- **Announce retry** — manual serial announce re-opens the port if it failed at startup
- **Receive logging** — logs when serial ANNOUNCE frames are received and peers discovered
- **Both peers required** — serial discovery only works when both nodes have the port open

### Shutdown
- **Port release** — SO_REUSEADDR on TCP/UDP/web binds; shutdown logs each transport closing
- **run.sh** — Ctrl+C waits for Python cleanup so ports are released before exit

## v0.1.37 (2026-07-03)

### Ports
- **Configurable ports** — Settings → Network: Web UI (9876), TCP (7825), UDP discovery (7826)
- **Strict ports** — enabled by default; no silent fallback to 9878/7827 when your chosen port is busy
- **Web port fix** — configured port is no longer overwritten when a fallback bind occurs

### Discovery
- **Announce** — restored v0.1.19-style identity + serial framing; kept Linux interface netmask fix

### Android CI
- **sdkmanager** — legacy `tools/bin/sdkmanager` symlink for Buildozer; NDK path verified before build

## v0.1.36 (2026-07-03)

### Discovery
- **Arch LAN fix** — interface list uses Linux ioctl with real netmasks (not /24 guess); primary LAN IP for announces
- **UDP discovery port** — broadcasts always target port 7826 plus the local bound port; announce payload includes `discovery_port`
- **Serial announce** — 5× burst with longer delay for RF links; announce buttons reflect whether transport is actually open

## v0.1.35 (2026-07-03)

### Discovery
- **Manual announce** — TCP/Serial buttons now validate transport availability and return clear errors when discovery cannot send (e.g. UDP socket down, serial port closed)
- **Announce feedback** — UI shows API error messages and confirms 3× burst; buttons disable when transport is unavailable

### Android CI
- **APK build restored** — runner SDK bootstrap with Buildozer symlink, `ANDROIDSDK`/`ANDROIDNDK` env vars, Python 3.12.8 pin; workflow fails if no APK is produced

## v0.1.31 (2026-07-03)

### File transfer
- **Receiver download link** — completed transfers show **Download file** / **Download folder ZIP** in chat
- **Folder send naming** — zipped folders arrive as `temp.zip`; collisions become `temp1.zip`, `temp2.zip`, …
- **Folder offer fix** — FILE_OFFER now uses the real folder name (not a temp zip path)

### Stability
- **Reconnect** — TCP timeouts during auto-reconnect no longer log unhandled task exceptions
- **Connect API** — connection timeouts return 503 instead of 500

### Android CI
- **APK build** — symlink runner SDK into Buildozer path; pin `android.sdk_path` / `android.ndk_path`

## v0.1.30 (2026-07-03)

### UI
- **Trusted contact menu** — scrollable on small screens; menu repositions to stay in viewport
- **Delete key** — select a trusted peer and press **Delete** to remove them

### Android
- **Target Android 15** — API 35, build-tools 35.0.0, arm64-v8a release APK
- **CI APK build** — fixed `grpmodule` compile failure (dropped armeabi-v7a; use runner SDK instead of `buildozer android update` bootstrap)

## v0.1.24 (2026-07-04)

### CI
- **Checks** — fix ruff lint (unused import, line length)
- **Android APK** — bootstrap SDK and accept licenses via `sdkmanager` before Buildozer build

## v0.1.23 (2026-07-03)

### Android CI
- **APK build** — pin P4A Android Python to **3.12.8** (avoids broken 3.14 default); pin `aiohttp==3.10.11`; clear stale P4A caches; upload build log on failure

## v0.1.22 (2026-07-03)

### Fixes
- **Send folder** — zipping runs off the event loop (chat stays responsive); temp zips use `~/.srltcp` instead of `/tmp`; clear errors for disk quota / oversized folders
- **Incoming files** — saved under the original filename; duplicates become `name (1).ext`, `name (2).ext`, …
- **Folder picker** — directories-only listing when sending a folder (faster browse)
- **Android CI** — auto-accept SDK licenses (`yes | buildozer`); pin build-tools 34

### UI
- **Send folder** button in chat header (next to attach file)

## v0.1.21 (2026-07-03)

### CI
- **Android APK build** — fixed Ubuntu 24.04 apt packages (`libtinfo6`, `libncurses-dev`); auto-accept SDK license; Rust + pinned Cython for P4A

## v0.1.20 (2026-07-03)

### Android — full rebuild (python-for-android)
- **Removed Chaquopy** — old `android/` Gradle/Chaquopy project deleted entirely
- **New P4A + Buildozer stack** — foreground `PythonService` runs `srltcp web`; `MainActivity` WebView loads localhost HTTPS UI
- **CI** — GitHub Actions builds APK on `main` push and release tags via Buildozer

### Fixes
- **Transfer dock** — closes when the current transfer completes; no longer reopens for unrelated background transfers
- **Serial settings panel** — flex layout no longer clipped on the left
- **Contact list** — hash ID removed from preview; **Copy hash ID** added to right-click menu

### New features
- **Send folder to peer** — right-click trusted contact → Send folder… (zipped E2EE transfer)

## v0.1.19 (2026-07-03)

### Fixes
- **Shared folder listing** — list API now waits for E2EE response and returns entries directly (no more stuck “Loading folder listing…”); auto-connects before list/offer; denied requests return empty listing with error
- **Receiver image preview** — incoming screenshots/images re-render as inline previews when transfer completes (not just a file attachment bubble)
- **Transfer dock** — progress bar hides after complete; polling no longer resurrects finished transfers; cancel (✕) blocked once transfer is done
- **Chat scroll** — new text messages scroll to bottom after image/file transfers
- **Android startup** — removed nested server thread (server runs in MainActivity worker thread only)

## v0.1.18 (2026-07-03)

### Critical fixes
- **Discovered peers invisible** — UI filtered 64-char hashes but real IDs are 32 hex chars; peers now appear after announce
- **Trusted/manual contacts broken** — hash validation corrected to 32 chars (Reticulum-style identity hash)
- **Android startup** — server starts directly from MainActivity (no longer blocked by denied notification permission); foreground service is optional keep-alive only

### New features
- **Add Contact** — manually trust a peer by hash ID with optional LAN host/port (no discovery required)
- **Copy hash ID** — click your profile hash to copy for sharing with another peer

## v0.1.17 (2026-07-03)

### Fixes
- **Trusted peers list** — invalid/duplicate entries filtered; generic placeholder names resolved
- **Shared folder download** — fixed path field bug; files now arrive via secure transfer
- **Receiver transfer UI** — progress bar removed on complete; **Save file** link injected dynamically
- **Android crash** — Python startup fallback, serial disabled on Android, safer WebView lifecycle

### New features
- **Share lifecycle** — time limits (1m–forever) and download count limits (1–unlimited)
- **Revoke shares** — sender can remove active share offers
- **Folder ZIP download** — directories compressed on sender before E2EE transfer
- **Media lightbox pan** — drag to move around zoomed images

### Performance
- TCP chunk size increased to 512 KiB (from 256 KiB) for higher throughput (~30–50+ MB/s on fast LAN)

## v0.1.16 (2026-07-03)

### New features
- **Drag-and-drop file send** — drop files from your file manager onto a contact in the sidebar to send to that peer
- **E2EE shared folders** — offer a local folder to a trusted, connected peer; browse and download over the encrypted link (no plaintext HTTP exposure)
- **Manual WAN peers** — add host/domain + port per trusted contact; connection modes `auto`, `lan`, or `wan`; outbound dials validated and rate-limited

### Fixes
- **Receiver transfer bar** — progress track removed when transfer completes; no stuck “transferring” state
- **Share / WAN WebSocket events** — `share_offer` and `share_listing` broadcast correctly to the UI

### Security
- WAN hosts must be public IPs or valid domains; private/localhost addresses rejected
- Share grants are recipient-bound with TTL; list/fetch denied without valid grant + handshake
- Settings flag documents intent to port-forward TCP **7825** (encrypted messaging port)

### Android
- APK **0.1.16** (versionCode 14) built via CI on tag `v0.1.16`

## v0.1.15 (2026-07-03)

### Fixes
- **UI flicker** — incremental transfer bubble updates instead of full chat re-render; throttled progress patches preserve image/video elements
- **Transfer dock** — hides immediately on complete/cancel for sender and receiver
- **Connection stability** — 45s post-transfer cooldown suppresses spurious link_down/reconnect; TCP keepalive; client ignores transient disconnects during cooldown
- **Media viewer** — zoom in/out/reset and mouse-wheel zoom in lightbox

### UX
- **Unread counters** — badge on trusted peers; cleared when chat is opened
- **Trusted peer list** — improved default contrast and readability
- **Notifications** — typed toasts; browser notifications when tab is in background

### Android
- WebView pause/resume lifecycle; service thread guard; no WebView destroy on rotation

## v0.1.14 (2026-07-03)

### Fixes
- **File transfers** — removed per-chunk `fsync` (major cause of stalls/disconnects on large files); fsync on complete only; file chunks processed asynchronously so the read loop stays responsive; serialized TCP sends via connection lock
- **Handshake** — client polls link status after connect; 15s server-side handshake wait; skip post-handshake ping during active transfers
- **HKDF** — `derive_session_key()` now requires explicit `info` (no legacy `srltcp-v1` default)
- **Media** — `Accept-Ranges: bytes` on transfer file endpoint for video seeking during partial downloads

### UI
- **Message actions** — copy (clipboard icon) and delete (trash icon) on text bubbles
- **Transfer dock** — auto-hides when idle; polls `/api/transfers`
- **Media in chat** — image/video preview, lightbox, download links with correct MIME types

### Android
- Foreground service + notification permission flow; server-ready wait before WebView; graceful FGS fallback

## v0.1.13 (2026-07-03)

### Fixes
- **File transfers** — ping suppressed during active transfers; link_down deferred while transferring; 10s link-wait retry mid-transfer; no compression on serial; forced reconnect blocked during transfer
- **Transfer dock** — auto-hides when no active transfers; polls `/api/transfers` for accurate state
- **Media in chat** — images and videos preview during transfer; click opens lightbox; Download link on all file types
- **Message actions** — copy (clipboard icon) and delete (trash icon) on text bubbles
- **Android APK** — `POST_NOTIFICATIONS` requested before foreground service; graceful fallback if FGS fails; service not stopped on background

## v0.1.12 (2026-07-03)

### Fixes
- **File transfers** — compressed chunk flag honored on receive; serial uses 8 KiB chunks; unique incoming paths
- **Transfer dock** — slim progress bar + MB/s only; auto-hides on complete/cancel/fail
- **Images in chat** — preview on sender and receiver during/after transfer; **Save as…** download link
- **Chat header** — TCP / SERIAL badge on active peer connection
- **Copy/Delete** — actions anchored on the message bubble (not opposite side)
- **Trusted contacts** — right-click opens contact menu (⋮ button removed)
- **Android APK** — foreground `SRLTCPService`, server-ready signal before WebView load

## v0.1.11 (2026-07-03)

### Fixes
- **Serial connect 500** — stale incomplete TCP links are torn down before serial dial; transport mismatch no longer sends handshake over dead TCP peer (`KeyError: unknown peer`)
- **File transfer transport** — offers and chunks use the active link's transport (serial vs TCP)
- **Transfer cancel** — `FILE_REJECT` notifies remote peer; both sides show **cancelled** in chat and transfer dock
- **CPU / temperature** — more accurate first CPU sample; temperature uses hottest CPU zone (not average)
- **HKDF session keys** — proper salt (`srltcp-session-salt-v2`) and directional info strings (`srltcp-v2-send` / `srltcp-v2-recv`)

### New / UI
- **Message actions** — Copy and Delete on text bubbles
- **Transport badges** — readable TCP / SERIAL pills on trusted and discovered contacts
- **Transfer dock** — inline above composer (no longer blocks chat); cancelled state with toast
- **Network map** — animated graph, legend, glow on active links
- **Android** — `set_android_data_dir()` before server start for correct app files path

### Project
- `SECURITY.md`, `CONTRIBUTING.md`, issue/PR templates, `.pre-commit-config.yaml`
- CI: ruff, mypy, pytest with coverage, advisory pip-audit

**Note:** Peers must both run v0.1.11+ for handshake compatibility after the HKDF change.

## v0.1.10 (2026-07-03)

### Fixes
- **Delete contact 405** — security middleware now allows `DELETE` and `PATCH` (trusted contact removal works)
- **File transfer stability** — chunk size reduced to 256 KiB with flow control; fewer disconnects on large files/images
- **Reconnect storm** — exponential backoff, skip during active transfers, cancel on successful handshake
- **Serial handshake** — retry handshake on incomplete links; connect API waits up to 12s for handshake
- **Stale send after reconnect** — messages/files auto-reconnect before send when link is down
- **Android APK** — loading screen, crash handler, 2048-bit TLS on device, longer server wait; `SRLTCP-0.1.10.apk`

### New
- **`./run.sh web --debug`** — verbose backend logging on Arch/Ubuntu/Windows
- **Settings window** — full-screen tabbed settings (General, Network, Serial, Folders, Clock, Advanced)
- **Transfer dock** — bottom progress bar with cancel on sender and receiver
- **Clock source** — sync from this machine or NTP server (configurable in settings)
- **`POST /api/transfers/{id}/cancel`** — cancel active file transfer

## v0.1.9 (2026-07-03)

### Fixes
- **Connection storm** — per-peer connect lock, duplicate TCP handshakes rejected, `force=false` by default; UI no longer auto-reconnects on every `link_down`
- **Link teardown** — only the active transport peer removes a link; stale disconnect events ignored
- **Setup folder browse** — browse modal z-index above setup overlay; delegated click handlers for setup wizard buttons
- **Trusted list** — deleting a contact updates UI immediately; trusted peers hidden from Discovered tab
- **Network map** — shows active links and dashed lines to discovered peers; includes linked nodes missing from discovery
- **Serial connect** — outbound dial path for serial transport in `connect_to_peer`
- **Status clock** — “Show clock” setting hides clock; time shown at top of sidebar (time only, timezone in settings)
- **Contact menu** — ⋮ submenu: clear chat, rename, block/unblock, delete
- **Peer notifications** — toast when remote peer connects or disconnects
- **Android APK** — `extractNativeLibs`, `buildConfig`, server-ready wait before WebView load; output as `SRLTCP-0.1.9.apk`

### API
- `PATCH /api/trusted/{hash_id}` — rename or block/unblock
- `POST /api/trusted/{hash_id}/clear-chat` — remove chat history for a contact
- `POST /api/connect` — `force` defaults to `false`

## v0.1.8 (2026-07-03)

### Fixes
- **LAN discovery** — UDP announce broadcasts to all interface subnet addresses (not only 255.255.255.255); 3-packet burst on manual announce
- **Serial announce** — uses `MessageType.ANNOUNCE` framing so peers on the RF link can decode discovery
- **Discovered peers** — TCP and serial identities listed separately with endpoint (IP:port or serial device)
- **Log spam** — quiet access logger suppresses `/api/system`, `/api/status`, `/api/peers` polling noise
- **UI polling** — reduced background fetch frequency; settings form no longer reloads interfaces on every status tick
- **Default baud** — 57600 (was 115200)
- **Android APK** — crash guards, WebView compat client, non-daemon server thread, app icon, output as `SRLTCP-0.1.8.apk`

### New
- **Timezone & clock** — timezone picker in settings; live clock in status bar
- **Setup browse** — folder browse buttons on first-run wizard (same as settings)
- **Network map** — settings → Network map visualizes self, peers, and active links

## v0.1.7 (2026-07-03)

### Fixes
- **Serial transport** — enabling serial in settings now starts the transport and shows the serial identity (no restart required)
- **Serial from settings** — `enable_serial` / port / baud loaded at startup from saved settings
- **Separate announces** — "Announce TCP" and "Announce Serial" buttons (independent discovery per transport)
- **Connection stability** — TCP disconnects detected correctly; auto-reconnect for trusted peers; `link_down` WebSocket events
- **Chat UI state** — link status syncs every 5s; no page refresh needed to send messages when encrypted/online
- **File transfer** — progress bar and MB/s in chat bubbles; files and images appear in the message window
- **Large file send** — requires active encrypted link; clearer errors when peer not connected
- **Delete contacts** — remove trusted peers from the Trusted tab (× button)
- **Android APK** — `SRLTCPApplication` for Chaquopy init; self-signed localhost TLS accepted; port fallback 9876–9878

### New
- **Uninstall scripts** — `uninstall.sh` / `uninstall.bat` remove config and app data
- **File download API** — `GET /api/transfers/{id}/file` for completed transfers in chat

## v0.1.6 (2026-07-03)

### Fixes
- **APK build** — stage `srltcp/` into `src/main/python` (avoids Gradle task validation failure from scanning `android/build`)

## v0.1.5 (2026-07-03)

### Fixes
- **APK build** — Chaquopy 15 requires one `pip.install()` call per package

## v0.1.4 (2026-07-03)

### Fixes
- **Ctrl+C shutdown** — process now exits promptly; WebSockets closed before aiohttp teardown; background loops awaited
- **APK build** — Chaquopy 15 `chaquopy` DSL (replaces deprecated `python` block); plugin versions in `settings.gradle.kts`
- **TCP transport stop** — close peer connections before server shutdown to avoid hang

## v0.1.3 (2026-07-03)

### Fixes
- **Handshake UI** — no longer stuck on "Handshaking…" after connect (race condition fixed)
- **Reconnect** — tearing down stale links before reconnect; force reconnect on each connect
- **Self in discovered** — own node filtered from peer list and purged from registry
- **Latency display** — RTT (ms) shown in chat header and peer list when connected
- **APK build** — Gradle wrapper, SDK licenses, NDK, Chaquopy buildPython fix

### New
- **Serial port picker** — dropdown lists USB devices plugged into the system
- **Baud rate picker** — selectable standard rates (9600–921600)
- **Disconnect API** — `POST /api/disconnect` for clean peer teardown

## v0.1.2 (2026-07-03)

### Fixes
- **Discovery spam removed** — "Discovered" toasts no longer fire every announce cycle; peers update silently in the list
- **Auto-announce respects settings** — when off, the announce loop stops; passive discovery still populates peers when others announce
- **Self-discovery filtered** — your own UDP broadcasts are no longer listed as discovered peers
- **File send fixed** — browser uploads files via multipart `/api/upload` before transfer

### New features
- **Ping / RTT** — measure network peer latency in milliseconds
- **Serial RF metrics** — link quality percentage and RTT for serial peers
- **Trusted peers** — must trust a discovered peer before messaging or sending files
- **Transfer progress** — progress bar with MB/s transfer speed
- **Settings panel** — TCP and serial settings, folder pickers, message retention presets
- **Identity management** — create, regenerate, or delete TCP and serial identities
- **Message retention** — 1 day, 1 week, 1 month, 1 year, forever, or until restart
- **Restart button** — restart the node from settings
- **Release notes** — click version badge (top-left) to view changelog

## v0.1.1

- HTTPS-only local web UI on 127.0.0.1
- Setup wizard and settings persistence
- Security middleware (CSP, HSTS)
- Graceful Ctrl+C shutdown
- CPU stats and port fallback

## v0.1.0

- Initial release: TCP + Serial P2P, E2EE messaging, file transfer, relay mode, web UI
