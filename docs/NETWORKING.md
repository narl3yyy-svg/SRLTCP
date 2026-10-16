# Networking guide

This page explains how SRLTCP connects two devices and what each setting under
**Settings → Network** does. Short version: **leave the defaults**. The app
picks the best path by itself and switches when your network changes.

**Phone to phone, nothing in between.** SRLTCP has no relays and no servers
that carry your messages, files or calls. Everything goes directly from one
device to the other. The only outside help is for *finding* each other:

- **Tor** (the doorbell): two phones on different networks reach each other
  over Tor just long enough to swap their current addresses, inside the
  end-to-end encrypted session. Nothing you send ever goes over Tor.
- **STUN**: a public server (Google's or Cloudflare's by default, or your own)
  tells each phone its own public IPv4 address and port. It learns your IP
  address, nothing else.

## How SRLTCP finds a path

| # | Path | When it works | What sees your traffic |
|---|---|---|---|
| 1 | **Direct, same network** | Both on the same Wi-Fi, or both on the same VPN (WireGuard) | Only the two devices |
| 2 | **Direct, across networks** | Two mobile carriers, a phone and a home Wi-Fi, two homes: the devices connect straight to each other over **UDP** through both NATs, over IPv4 and IPv6 at once (see below); also one on the VPN and the other on the home Wi-Fi | Only the two devices |
| — | **Tor (doorbell only)** | Finds the contact on any network so path 2 can be set up; carries no messages, files or calls (both devices need *Find contacts on other networks (Tor)* on) | Tor relays carry the sealed setup messages; your contact doesn't see your IP over Tor |

Every path carries the same end-to-end encrypted session (signed, quantum-safe
handshake, forward secrecy). The chat header shows **DIRECT**, or
**CONNECTING** while a contact found over Tor is being connected directly.
Messages and files you send meanwhile wait and go out automatically.

## Common setups

**Both phones on the same Wi-Fi or the same VPN.** Works out of the box. Pair
once (QR, code or NFC) and you're done.

**One phone on the VPN, the other on the home Wi-Fi.** Works directly, as long
as the VPN can reach the home network. For a home WireGuard server, that means
the VPN phone's `AllowedIPs` include the home LAN, for example
`10.0.100.0/24, 192.168.1.0/24`. The two phones tell each other their addresses
(LAN and VPN) inside the encrypted session, so neither has to type anything.
It's fine if only the VPN phone can open the connection: it dials in as soon as
the VPN connects.

**Two different networks (mobile data on two carriers, a friend's Wi-Fi).**
Pair once (on the same Wi-Fi, or with a pairing code), keep Tor and *Direct
internet connections* on (default) on both phones. They find each other over
Tor and connect directly, usually within seconds.

## Tor: the doorbell

Each device runs Tor and publishes a private *onion service*. Contacts on
other networks reach each other through it, from any Wi-Fi, mobile data or
VPN, behind any router or carrier NAT. **Tor is only used to find each other
and set up a direct connection**: over it go the handshake, the address swap
and the start of a punch round, nothing else. Messages, files, calls, groups,
shares and sync **never go over Tor**.

- **Turn it on:** *Settings → Network → Find contacts on other networks
  (Tor)*, on both devices. It's on by default.
- **Android:** Tor is built into the app.
- **PC:** install [Tor Browser](https://www.torproject.org/download/). SRLTCP
  finds its `tor.exe` and starts its own copy in the background.
- **First connection:** the two devices must have met once (pairing on the same
  Wi-Fi/VPN, or with a pairing code). That's how they swap onion addresses,
  inside the encrypted connection.

Privacy: only your trusted contacts get your onion address, and connections
through it are only accepted from trusted contacts. A stranger who learns the
address gets no session, not even a contact request.

## Direct connections across networks (hole punching)

Routers and mobile carriers block incoming connections, but they let replies
to *outgoing* traffic back in. If both phones send to each other at the same
moment, each NAT sees the other phone's packets as replies, and a direct
connection opens. Nothing on either router needs to be configured. This is the
standard technique video-call apps use (ICE/STUN, RFC 8445/5389).

1. Each phone asks a **STUN server** which public IPv4 address and port the
   internet sees for its UDP socket (port **7828**). IPv6 needs no STUN: the
   phone's own global address works (temporary privacy addresses; never one
   built from the hardware address).
2. Over Tor, the phones swap those addresses, a random key share each and a
   connection id, **inside the end-to-end encrypted session**, so only your
   contacts ever see them. Each message says whether the sender already has the
   contact's addresses, so a swap never gets stuck halfway.
3. Both phones send a short "priming" packet that opens their own NAT, then
   probe each other's IPv4 and IPv6 addresses at once. The first packet that
   arrives and decrypts fixes the path.
4. The normal signed, quantum-safe handshake runs over the new connection (an
   impostor can't take it over) and replaces the Tor session. What waited goes
   out at once.

**Home Wi-Fi and mobile data.** Many carriers give a new public port for
every destination, so the port a mobile phone learnt from STUN isn't the one
its packets come from, and most home routers only let in replies from the
exact address and port the phone inside sent to: neither side's packets get
through, while two carriers connect fine. So a phone on a Wi-Fi asks its own
router to forward the direct-connection port (**UPnP IGD** or **NAT-PMP**,
the way video-call and game apps do) and offers that address first. The
mobile phone's packets then get in from whatever port its carrier picked,
the first authenticated one fixes the path, and the home phone answers it.
Only routers on your own Wi-Fi are asked; the mapping points only at the
phone's UDP socket (which drops anything that isn't an authenticated packet
from a contact it's connecting to), expires by itself and is removed when the
app stops. Routers with UPnP and NAT-PMP turned off still work for
everything else. *Settings → Network → Open a port on my home router*
(on by default).

A round that fails is retried after 4 s from a fresh UDP port (routers can
hold stale state for the old pair of ports for ~30 s), then after 15, 30, 60
and 120 s, and at once whenever either phone's network changes.

**On the wire**, every UDP packet is encrypted and authenticated (AES-256-GCM)
with keys only the two contacts know; packets that don't decrypt are dropped
without an answer, so nobody can inject data, close the connection or use the
phone to bounce traffic. The session inside adds its own end-to-end
encryption. The connection is reliable and ordered like TCP, and call
audio/video go as separate datagrams that never wait behind lost file data.
Chat, file confirmations and other small messages go out ahead of queued file
data.

**Speed on mobile networks.** The sending rate follows BBR, the congestion
control YouTube and QUIC use on mobile: it measures how fast data actually
arrives and paces packets at that rate, so the random losses of radio links
don't slow it down. `scripts/bench_udp_path.py` emulates mobile paths:

| Path (each way) | Link | Speed |
|---|---|---|
| LTE, 90 ms round trip, 0.2 % loss | 2.5 MB/s | 2.25 MB/s |
| LTE, 140 ms, 1 % loss | 2.5 MB/s | 2.15 MB/s |
| 5G far away, 200 ms, 0.5 % loss | 7.5 MB/s | 5.25 MB/s |
| congested cell, 100 ms, 2 % loss | 0.62 MB/s | 0.56 MB/s |

**One session at a time.** While the direct connection works, a session the
contact opens over Tor is refused; only after the direct one has been silent
for 15 s does Tor ring again. If a phone's address changes (Wi-Fi to mobile
data), authenticated packets from the new address move the connection there.

### What can't connect directly

With no relay, some network combinations can't be connected, by this or any
other app; messages then wait (nothing goes over Tor) and go out as soon as it
works:

- **"Hard" (symmetric) NAT on both sides.** Some carriers give a new random
  public port for every destination. When *both* phones are behind such a
  NAT, there is no address either one could tell the other. **Fixes:** one of
  the phones on Wi-Fi (most home and public Wi-Fi routers are "easy"), or IPv6
  on both (most US and EU carriers offer it on mobile data; on Android check
  *Settings → Network → SIM → Access Point Names → APN protocol: IPv4/IPv6*).
- **Mobile data with a hard NAT, and a home router with UPnP and NAT-PMP
  turned off.** The report on the home phone says "Home router port
  mapping: router doesn't offer UPnP / NAT-PMP". **Fix:** turn UPnP on in
  the router's settings.
- **A Wi-Fi that keeps devices apart** (plane, hotel, guest Wi-Fi) for two
  phones on that same Wi-Fi, or both behind one public address (Starlink).
- **Networks that block UDP** (some corporate and guest networks).
- **Turned off.** With *Direct internet connections* off (no STUN, contacts
  never learn your IP), only the same Wi-Fi or VPN connects.

*Edit contact → Check connection* tells you which case you're in, and **Copy
connection report** gives a summary to send along (the NAT type of this
network, which address kinds each side offers, how recent attempts ended; no
IP addresses).

Tested in `scripts/simulate_udp.sh`: two phones behind separate carrier NATs,
TCP between the networks blocked, 0.5 % loss, a private Tor network as the
doorbell: found over Tor and delivered directly 11 s after the phones split,
a 40 MB file at 7.4 MB/s with chat flowing meanwhile; with symmetric NAT on
both sides the message waits. `scripts/simulate_nat.sh` covers two home
routers and a VPN phone with a home-Wi-Fi phone.

## Settings, one by one

### Over the internet

| Setting | What it does | Default |
|---|---|---|
| **Direct internet connections** | Phone-to-phone UDP across networks, described above. Shares your public IP address **only with your contacts** (and a STUN server), inside the encrypted session. Off: only the same Wi-Fi or VPN; contacts never see your IP. | On |
| **Open a port on my home router** | On Wi-Fi, asks your router (UPnP / NAT-PMP) to forward the direct-connection UDP port to this phone, so a contact on mobile data gets through. Only your own router is asked; removed when the app stops. | On |
| **STUN servers** | Who tells this phone its public IPv4 address. `host:port`, separated by commas; empty for the defaults. | Google, Cloudflare |
| **Find contacts on other networks (Tor)** | The doorbell, described above: only to find each other and set up the direct connection. | On |

### Same Wi-Fi / VPN

| Setting | What it does | Default |
|---|---|---|
| **Network interface / IP** | Which of your addresses to advertise on the local network. Leave on *Auto* unless the wrong one is chosen. | Auto |
| **Announce myself at startup** | Sends a signed "I'm here" message to the LAN/VPN at startup. It shares your name and ID with that network. | Off |
| **Network map** | Picture of your devices, contacts and the path to each. | — |

### More network options

| Setting | What it does | Default |
|---|---|---|
| **Web UI port** | The local, token-protected HTTPS page the app shows. Only reachable from the device itself. | 9876 |
| **TCP peer port** | Where contacts connect to you. Must be the same on all your devices. Direct connections across networks use this port + 3. | 7825 |
| **UDP discovery port** | Used to find devices on the LAN/VPN. Same on all your devices. | 7826 |
| **Use exact ports** | If a port is busy, fail instead of quietly using the next one. | On |
| **Tor program** | PC only: full path to `tor.exe` if SRLTCP can't find Tor Browser's. | Empty (auto) |
| **I port-forward TCP 7825** | Optional manual setup; direct connections work without it. | Off |

## Ports at a glance

| Port | Protocol | Where | Needs opening? |
|---|---|---|---|
| 7825 | TCP | Each device (peer connections) | Only inside your LAN/VPN firewall, never on the router |
| 7826 | UDP | Each device (LAN/VPN discovery) | Same as above |
| 7828 | UDP | Each device (direct connections between networks) | No (on Wi-Fi the app asks the router itself, UPnP / NAT-PMP). It only answers authenticated packets from contacts it's connecting to |
| — | Tor | Each device (onion service) | No. Tor only dials out, and the onion listener is bound to 127.0.0.1 |
| 9876 | TCP | Each device (local web UI, 127.0.0.1 only) | No |

## Privacy summary

- Your IP address is visible to your contacts and the STUN server **only if**
  *direct internet connections* is on, and to the networks between you once
  you're connected directly. Strangers can't learn it from the app: addresses
  travel only inside encrypted sessions with trusted contacts.
- Over Tor (the doorbell), your contact doesn't see your IP address.
- On the LAN/VPN, discovery announcements (your name and ID) are visible to
  devices on that network. They're signed, so they can't be faked.
- Discovery never leaves your own networks: nothing is announced on mobile
  data or over the internet.

## Troubleshooting

| Symptom | Check |
|---|---|
| Not sure why a contact won't connect | *Edit contact* → **Check connection**, then **Copy connection report**. |
| Different networks never connect | Tor on and showing "ready" on both devices (Settings → Network), and paired once. |
| Header stays on **CONNECTING** | The phones found each other but can't connect directly: the report says why (most often hard NAT on both carriers). One phone on Wi-Fi, or IPv6 on both, fixes it. |
| Tor stays at "connecting…" | The network may block Tor. On a PC, check the clock is correct. |
| VPN phone can't reach the home phone | The VPN client's `AllowedIPs` must include the home LAN. |
| Nothing works after changing ports | Every device must use the same peer and discovery ports. Restart the app. |
