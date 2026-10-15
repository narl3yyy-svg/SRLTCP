# Networking guide

This page explains how SRLTCP connects two devices and what each setting under
**Settings → Network** does. Short version: **leave the defaults**. The app
picks the best path by itself and switches when your network changes.

## How SRLTCP finds a path

For each contact, the app tries these in order and uses the first that works:

| # | Path | When it works | What sees your traffic |
|---|---|---|---|
| 1 | **Direct, same network** | Both on the same Wi-Fi, or both on the same VPN (WireGuard) | Only the two devices |
| 2 | **Direct, across networks** | Two mobile carriers, a phone and a home Wi-Fi, two homes: the devices punch a direct **UDP** connection through both NATs (see below); also one on the VPN and the other on the home Wi-Fi | Only the two devices |
| 3 | **Internet relay** | Always, as long as both devices can reach your relay | The relay forwards sealed packets it can't read |
| 4 | **Tor** | Any network that allows Tor, no server and no port forwarding (both devices need *Reach contacts anywhere over Tor* on) | Tor relays carry sealed traffic; your contact doesn't see your IP, and no single Tor relay knows both who you are and who you talk to |

Every path carries the same end-to-end encrypted session (signed handshake,
forward secrecy). A path is only a pipe; changing paths never weakens
encryption. While on the relay, the app keeps trying to get back to a direct
path, every 45 seconds per contact, and moves over as soon as one works (the
same goes for Tor).

The badge in the chat header shows which path a contact is on: **DIRECT**,
**RELAY** or **TOR**. When a contact drops off (for example they leave the
Wi-Fi), the chat says so within about 30 seconds, then shows "Connected again"
once the app has found another path. Messages and files you send meanwhile wait
and go out automatically.

**Why "public addresses" alone don't connect two networks.** Two devices on
different networks can't find each other without a meeting point: something both
can reach. Direct internet connections (hole punching) use the session you
already have, over **Tor** or your **relay**, as that meeting point to swap
public addresses: Tor is the doorbell, the direct UDP connection carries the
traffic. No server of your own is needed: a public STUN server tells each
phone its public address (see below).

## Common setups

**Both phones on the same Wi-Fi or the same VPN.** Works out of the box. Pair
once (QR, code or NFC) and you're done.

**One phone on the VPN, the other on the home Wi-Fi.** Works directly, as long
as the VPN can reach the home network. For a home WireGuard server, that means
the VPN phone's `AllowedIPs` include the home LAN, for example
`10.0.100.0/24, 192.168.1.0/24`. The two phones tell each other their addresses
(LAN and VPN) inside the encrypted session, so neither has to type anything.

It's fine if only the VPN phone can open the connection. That's common when
WireGuard runs on a separate box and the home LAN can't reach VPN addresses. The
VPN phone dials in as soon as the VPN connects, then every 30 s for the first few
minutes, and anything the home phone queued is delivered then. If the VPN can't
reach the LAN at all, the phones use the relay. *Check connection* (in Edit
contact) shows which case you're in.

**Two different networks (friend's house, mobile data), no port forwarding.**
1. Run a relay on any server with a public address, for example a cheap VPS
   or the machine that runs your WireGuard. See
   [INTERNET_RELAY.md](INTERNET_RELAY.md). Only that server needs an open
   port. Neither home router needs any change.
2. Paste the relay code on **one** phone. Contacts get it automatically.
3. Messages, files and calls now work anywhere, through the relay.
4. With **Direct internet connections** on, the phones also try to connect
   directly through both home routers (hole punching). When that works, the
   relay drops out of the path.

## Tor: reach contacts from any network, no server needed

Each device runs Tor and publishes a private *onion service*. Your contacts
connect to it through the Tor network. That works from any Wi-Fi, mobile data
or VPN, behind any router or carrier NAT, and needs no server and no port
forwarding.

- **Turn it on:** *Settings → Network → Reach contacts anywhere over Tor*, on
  both devices. It's on by default.
- **Android:** Tor is built into the app.
- **PC:** install [Tor Browser](https://www.torproject.org/download/). SRLTCP
  finds its `tor.exe` by itself and starts its own copy in the background; you
  don't need to keep Tor Browser open. The Tor Expert Bundle, or `tor` on
  PATH, works too. Set *More network options → Tor program* if it's somewhere
  unusual.
- **First connection:** the two devices must have met once (pairing on the same
  Wi-Fi/VPN, or over your relay). That's how they swap onion addresses, inside
  the encrypted connection.
- **When it's used:** only when there's no faster path (same network, VPN,
  IPv6, relay). The app keeps checking and moves to a direct path as soon as
  one works. If both devices have IPv6 internet, they agree over Tor to dial
  each other directly (hole punching, below) and leave Tor for a direct
  connection; if the network blocks that, they stay on Tor.
- **Speed:** fine for messages, slow for big files. Tor lets each connection
  have only about 250 KB in flight per round trip, and onion round trips take
  0.5-1 s, so one Tor circuit carries roughly 0.3 MB/s however fast your
  internet is. A file is therefore spread over 8 extra circuits ("lanes",
  each routed separately, opened for file data only and closed a minute
  after the last file): typically 1-2 MB/s, depending on the Tor relays you
  get. Messages and calls stay on the main circuit and don't queue behind the
  file. Calls work but can be choppier than on a direct path.
  The file bubble shows how many circuits carry it ("Tor ×9" = main + 8).
  The sender opens the circuits; if it doesn't know the receiver's onion
  address yet (the receiver reached it over Tor first), it asks and the
  receiver opens them, and data flows over circuits opened by either side.
  A circuit that gets stuck holds up the whole file, so when nothing is
  confirmed for 6 seconds the chunks it holds go out on the other circuits
  and a fresh one replaces it. A contact on an older version that accepts
  fewer circuits is not asked for more.
- **Leaving Tor for speed:** as soon as a file starts over Tor, both devices
  look for a direct path (same Wi-Fi, VPN, IPv6), then every 15 seconds
  while it's moving. When one works, the session moves over in the middle of
  the file and carries on from where it was, often 10-50× faster.

Privacy: only your trusted contacts get your onion address, and connections
through it are only accepted from trusted contacts. A stranger who somehow
learns the address gets no session, not even a contact request. Over Tor,
your contact doesn't learn your IP address and you don't learn theirs. The
first Tor relay sees your IP but not who you talk to, and no relay sees
both. Your internet provider can see that you use Tor, but not with whom or
what. Exception: with *Try direct internet connections* on (default), your
contacts learn your IPv6 address so the two of you can connect directly, and
once you're on that direct connection, your networks (Wi-Fi owner, mobile
carrier) can see which IP address you're talking to, as with any direct
connection. The content stays end-to-end encrypted. Turn the setting off to
stay on Tor: then neither contacts nor networks learn who talks to whom.

## Hole punching (direct internet connections)

Routers and mobile carriers block incoming connections, but they let replies
to *outgoing* traffic back in. If both phones send to each other at the same
moment, each NAT sees the other phone's packets as replies, and a direct
connection opens. Nothing on either router needs to be configured. This is how
video-call apps get through, and SRLTCP does it over **UDP**, which carrier
NATs let through far more often than TCP.

How it works in SRLTCP:
1. Each phone asks a **STUN server** (Google's and Cloudflare's by default, or
   your own under *STUN servers*) which public address and port the internet
   sees for its UDP socket (port **7828**). IPv6 addresses need no STUN.
2. Over the session they already have (Tor or the relay), the phones swap
   those addresses, a random key share each and a connection id, **inside the
   end-to-end encrypted session**, so only your contacts ever see them.
3. Both phones first send a "priming" packet that opens their own NAT but
   expires before reaching the other one (a hop limit), then probe each other.
   The first packet that arrives and decrypts fixes the path.
4. The normal signed, quantum-safe handshake runs over the new connection (an
   impostor can't take it over) and it replaces the Tor/relay session. Files,
   chat and calls move over without restarting; a file carries on from where
   it was.

On the wire, every UDP packet is encrypted and authenticated (AES-256-GCM)
with keys only the two contacts know; packets that don't decrypt are dropped
without an answer, so nobody can inject data, close the connection or use the
phone to bounce traffic. The session inside adds its own end-to-end
encryption. The connection is reliable and ordered like TCP (acknowledgements,
retransmission, congestion control that watches delay as well as loss, so chat
stays snappy during a big file), and call audio/video go as separate
datagrams that never wait behind lost file data. If a phone's address changes
(Wi-Fi to mobile data), authenticated packets from the new address move the
connection there; if it goes quiet for 30 seconds, Tor or the relay takes over
and a new round starts.

When it doesn't work, Tor or the relay simply keeps carrying the traffic:
- **"Symmetric" carrier NAT** gives a new random port for every destination,
  which no hole punching can predict. *Check connection* tells you when your
  network does this. Wi-Fi on either side, or IPv6 on mobile data, usually
  works.
- **Some corporate or guest networks** block UDP.
- **Turned off.** Turning off *Direct internet connections* stops it (and
  STUN), so contacts never learn your IP address (Tor or relay only).

Tested in `scripts/simulate_udp.sh`: two phones behind separate carrier NATs,
TCP between the networks blocked, 0.5% packet loss: direct UDP after about
10 s, a 40 MB file at 14-21 MB/s with chat flowing meanwhile; with symmetric NAT
on both sides they correctly stay on the relay.

**TCP and IPv6.** The app also tries TCP hole punching (through home routers,
with a relay telling it its public IPv4 address) and plain IPv6. Addresses
built from the phone's hardware (MAC) address are never shared; the temporary
privacy addresses are used instead.

## Settings, one by one

### Over the internet

| Setting | What it does | Default |
|---|---|---|
| **Use the internet when there's no LAN/VPN path** | Uses the relay when there's no direct path. Off means LAN/VPN only. | On |
| **Relay code** | `srltcp-relay://host:port#…`, printed by `srltcp relay`. It contains the server address, the certificate fingerprint (so nobody can impersonate the relay) and the access key. Paste it on one phone; contacts receive it inside the encrypted session. Keep it private. | Empty |
| **Try direct internet connections** | Hole punching (IPv6 from a Tor session, IPv4 via your relay), described above. Shares your public IP address **only with your contacts**, inside the encrypted session. Off: Tor or relay only, contacts never see your IP. | On |
| **Status** | Which relays you're connected to, how many contacts use each one, and your public address as the relay sees it. This is shown only to you. | — |
| **Reach contacts anywhere over Tor** | Tor onion service, described above. No server or port forwarding needed. Your Tor address goes only to trusted contacts, and only they can connect through it. | On |

### Same Wi-Fi / VPN

| Setting | What it does | Default |
|---|---|---|
| **Network interface / IP** | Which of your addresses to advertise on the local network. Leave on *Auto* unless the device has several networks and the wrong one is chosen. | Auto |
| **Announce myself at startup** | Sends a signed "I'm here" message to the LAN/VPN at startup, so nearby devices see you without pressing *Announce*. It shares your name and ID with that network. | Off |
| **Network map** | Picture of your devices, contacts and the path to each. | — |

### More network options

Change these only if you know you need to.

| Setting | What it does | Default |
|---|---|---|
| **Web UI port** | The local, token-protected HTTPS page the app shows. Only reachable from the device itself. | 9876 |
| **TCP peer port** | Where contacts connect to you. Must be the same on all your devices. Hole punching uses this port + 3. | 7825 |
| **UDP discovery port** | Used to find devices on the LAN/VPN. Must be the same on all your devices. | 7826 |
| **Use exact ports** | If a port is busy, fail instead of quietly using the next one. That keeps devices compatible. | On |
| **External call relay (TURN/STUN server)** | **Leave empty.** Calls already travel inside SRLTCP's own encrypted connection, so they work wherever messages do. Use this only if you run your own TURN server (for example *coturn*) as a last resort: enter `turn:your.server:3478`, and the **TURN username and password** fields appear. Fill in the username and password you configured on that server. Calls stay end-to-end encrypted. | Empty |
| **Tor program** | PC only: full path to `tor.exe` if SRLTCP can't find Tor Browser's by itself. | Empty (auto) |
| **I port-forward TCP 7825** | The old manual setup for direct internet connections. The relay and hole punching make it unnecessary. | Off |

## Contact address fields (Edit contact)

You normally never touch these, because the app learns addresses by itself.

| Field | Meaning |
|---|---|
| **Address — LAN or VPN IP** | The address the app dials first. It's updated automatically to whatever worked last. Set it by hand only if discovery can't find the contact (for example `10.0.100.5` on WireGuard). |
| **Port** | That contact's peer port (7825 unless they changed it). |
| **Also tried** (under the address) | Other addresses this contact told you about, such as their LAN and VPN addresses. They're all dialed at once, and the first to answer wins. |
| **Internet address (advanced)** | A public host or domain where the contact port-forwards their peer port. This is the old manual setup. With a relay you don't need it. |
| **Connection mode** | *Auto*: address first, then internet address. *Address only* / *Internet address only* force one of them. The relay is used in any mode when nothing direct works. |

## Ports at a glance

| Port | Protocol | Where | Needs opening? |
|---|---|---|---|
| 7825 | TCP | Each device (peer connections) | Only inside your LAN/VPN firewall, never on the router |
| 7826 | UDP | Each device (LAN/VPN discovery) | Same as above |
| 7827 | TCP | **Relay server only** | Yes, on the server's firewall (or run it on 443) |
| 7828 | UDP | Each device (direct connections between networks) | No. It only answers packets from contacts it's punching to; everything else gets no reply |
| 7828 | TCP | Each device (TCP hole punching, outgoing) | No, it only dials out |
| — | Tor | Each device (onion service) | No. Tor only dials out, and the onion listener is bound to 127.0.0.1 |
| 9876 | TCP | Each device (local web UI, 127.0.0.1 only) | No |

## Privacy summary

- Your IP address is visible to: the relay operator (you, if you run it), and
  your contacts and the STUN server **only if** *direct internet connections*
  is on (the STUN server learns only that this IP asked, nothing about your
  contacts or messages). Strangers
  can't learn it from the app, because addresses travel only inside encrypted
  sessions with trusted contacts.
- Over Tor, your contact doesn't see your IP address. The first Tor relay sees
  your IP but not who you talk to.
- The relay never sees names, identity keys, message types or content, or
  which of your contacts you're talking to. It sees IP addresses, timing and
  amounts of data.
- On the LAN/VPN, discovery announcements (your name and ID) are visible to
  devices on that network. They're signed, so they can't be faked.
- Discovery never leaves your own networks: nothing is announced on mobile
  data or over the internet, and announces, *Find by IP* and contact requests
  from other networks are ignored. Someone elsewhere can't find you; to add
  them, use a pairing code or link.

## Troubleshooting

| Symptom | Check |
|---|---|
| Not sure why a contact won't connect | *Edit contact* → **Check connection**: it tries every address the app knows for them and tells you what works and what to change. |
| Different networks never connect, only the same Wi-Fi works | There's no meeting point. Turn on *Reach contacts anywhere over Tor* on both devices (on a PC, install Tor Browser first), or set up a relay. Then pair once while on the same network. |
| Messages wait while one phone is on mobile data and the other on Wi-Fi | Open *Edit contact* → **Check connection**. It says whether Tor is working on this device and whether the contact has shared a Tor address yet. If not, open the chat once while both phones are on the same Wi-Fi, with Tor showing "connected" on both (Settings → Network). The chat also explains it by itself if a contact stays unreachable. |
| Tor stays at "connecting…" | The network may block Tor (some workplaces and schools do). Check the PC's clock is correct; Tor refuses to work with a wrong clock. |
| Contact always shows **relay**, never direct | Normal on mobile data (carrier NAT). On home networks, make sure *Direct internet connections* is on for both phones. Some routers block hole punching. |
| Contact always shows **TOR**, never direct | *Edit contact* → **Check connection** → *Speed* says why: STUN blocked, the contact on an older version, direct connections off, or a "symmetric" carrier NAT. Both phones need this version and *Direct internet connections* on. |
| VPN phone can't reach the home phone directly | The VPN client's `AllowedIPs` must include the home LAN, and the WireGuard server must route or NAT to the LAN. |
| "Relay unreachable" | Is the server up, TCP 7827 open on its firewall, and the code pasted correctly? |
| "Relay certificate doesn't match" | The code is from a different relay, the relay's certificate was regenerated, or something is intercepting the connection. Get a fresh code from `srltcp relay`. |
| Nothing works after changing ports | Every device must use the same peer and discovery ports. Restart the app after changing them. |
