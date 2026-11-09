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
| 3 | **Serial radio** | No network at all: two phones with USB serial radio modems in range of each other. Comes before path 2 (internet) when both are possible, after path 1 (same Wi-Fi/LAN). See the README's *USB Serial P2P* | Anyone in radio range sees that something is sent, never what (sealed with the session keys) |
| — | **Tor (doorbell only)** | Finds the contact on any network so path 2 can be set up; carries no messages, files or calls (both devices need *Find contacts on other networks (Tor)* on) | Tor relays carry the sealed setup messages; your contact doesn't see your IP over Tor |

Every path carries the same end-to-end encrypted session (signed, quantum-safe
handshake, forward secrecy). The chat header shows **DIRECT**, **SERIAL**
(the radio), or **CONNECTING** while a contact found over Tor is being
connected directly.
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

**Two devices on the VPN from two different networks** (a laptop at work and a
phone on mobile data, both on the home WireGuard). They connect directly over
the tunnel. Each learns the other's VPN address inside the encrypted session
(over Tor) and keeps it. Without Tor (a *quiet* work network), type the other
device's VPN address in **Find by IP** once: it dials that contact at once, and
the other device dials back. For this to work the WireGuard server must pass
traffic **between** two clients, see *VPN checklist* below.

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
- **Android:** Tor is built into the app. It runs in its own small process,
  bound to the app (Android always allows that, and a slow first start
  survives); if it stops or fails to start, the app ends that process and
  starts a fresh Tor by itself. If Tor can't start or connect, Settings
  shows Tor's own reason (from its log). While it connects, *Settings → Network* shows Tor's
  own progress and any problem it reports (e.g. "Connection refused" on a
  network that blocks Tor).
- **Desktop app (Windows, macOS):** Tor is built in; nothing to install.
- **From source on Windows, macOS and Linux:** the first time you start SRLTCP with
  `run.bat web` (Windows) or `./run.sh web` (macOS, Linux) and no Tor is
  found, it asks in the terminal whether to download the **Tor Expert
  Bundle** (the Tor program only, ~20 MB) from torproject.org, built for
  your computer: Windows x86_64, macOS on Apple silicon (arm64; Intel Macs
  get x86_64), Linux x86_64 or arm64. Say yes: the newest stable version is
  downloaded over HTTPS, checked against the SHA-256 checksum the Tor
  Project publishes, and unpacked into `SRLTCP/tor-expert-bundle`. Say no
  and you're not asked again (`run.bat tor-setup` or `./run.sh tor-setup`
  downloads it later). Started without a terminal (a service), it never
  asks.
- **PC (any):** or install [Tor Browser](https://www.torproject.org/download/)
  on Windows or macOS, or the `tor` package on Linux (`sudo apt install tor`).
  SRLTCP finds it and starts its own copy in the background. On Windows it
  looks in Tor Browser's usual places (Desktop, Program Files, AppData), for
  the **Tor Expert Bundle** extracted in Downloads, on the Desktop or in the
  SRLTCP folder (`SRLTCP\tor\tor.exe`), and on PATH. On macOS: PATH,
  `SRLTCP/tor-expert-bundle`, Homebrew (`/opt/homebrew`, then `/usr/local`)
  and Tor Browser in Applications. On Linux: PATH, `SRLTCP/tor-expert-bundle`,
  `/usr/bin`, `/usr/sbin` and Tor Browser in your home, Downloads or Desktop. Anywhere else, put the
  path of `tor.exe` (or the folder it's in) in *Settings → Network → Tor
  path*. Restart SRLTCP after installing.
- **First connection:** the two devices swap onion addresses inside their
  encrypted connection, so normally they meet once first (the same Wi-Fi/VPN,
  or a pairing code). **Never met?** Each copies its Tor address from under its
  hash ID at the top of the contact list (tap it; shown once Tor is
  connected) and sends it to the other with the hash ID; each adds the other
  with *Add contact → Hash ID + Tor address*. Both must add each other: a Tor
  connection from someone who isn't your contact is refused. After that the
  addresses stay up to date by themselves.

Privacy: the app gives your onion address only to your trusted contacts (or
you copy it yourself), and connections through it are only accepted from
trusted contacts. A stranger who learns the address gets no session, not even
a contact request, but can tell when your device is online, so share it like
a phone number. *Settings → Network → New Tor address* replaces it: contacts
you're connected to get the new one at once; anyone else needs it again. A
wrong address for a contact can't let anyone impersonate them: the signed
handshake still checks their keys.

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
app stops. **Off by default since 0.15** (it lets packets from the internet
reach the port, and security software reports the UPnP traffic): turn on
*Settings → Network → Open a port on my home router* if a contact on mobile
data can't reach you at home.

**No scanning.** SRLTCP only ever sends to addresses a contact gave it, a few
packets at a time. (0.13.9–0.14.9 had a "birthday punch" for a strict NAT on
one side that probed thousands of random ports of the contact's address;
security software rightly reads that as a port scan, so it was removed in
0.15.)

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
- **A strict NAT on one side** without router port mapping: turn on *Open a
  port on my home router* on the phone at home, or use IPv6.
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
| **Open a port on my home router** | On Wi-Fi, asks your router (UPnP / NAT-PMP) to forward the direct-connection UDP port to this phone, so a contact on mobile data gets through. Only your own router is asked; removed when the app stops. | Off |
| **STUN servers** | Who tells this phone its public IPv4 address. Leave it empty: the defaults (`stun.l.google.com:19302`, `stun.cloudflare.com:3478`) work everywhere. To use others: `host:port`, separated by commas (at most 4), e.g. `stun.nextcloud.com:443`. DNS servers such as 8.8.8.8 are not STUN servers and are refused. | Google, Cloudflare |
| **Find contacts on other networks (Tor)** | The doorbell, described above: only to find each other and set up the direct connection. | On |
| **Quiet networks** | Networks (`10.20.0.0/16, …`) where SRLTCP uses **none** of the above: no Tor, no STUN, no direct connections across networks, no router port mapping. For work or school networks whose firewall or antivirus would flag them. *Make the network I'm on quiet* fills it in. Same-Wi-Fi/VPN contacts and the radio still work there. | None |

### Same Wi-Fi / VPN

| Setting | What it does | Default |
|---|---|---|
| **Network interface / IP** | Which of your addresses to advertise on the local network. Leave on *Auto* unless the wrong one is chosen. | Auto |
| **Announce** (button) | The only way devices are found: sends one signed "I'm here" to the Wi-Fi/LAN and VPN, and for 10 minutes answers devices that announce too. SRLTCP never announces or scans by itself. | — |
| **Network map** | Picture of your devices, contacts and the path to each. | — |

### More network options

| Setting | What it does | Default |
|---|---|---|
| **Web UI port** | The local, token-protected HTTPS page the app shows. Only reachable from the device itself. | 9876 |
| **TCP peer port** | Where contacts connect to you. Must be the same on all your devices. Direct connections across networks use this port + 3. | 7825 |
| **UDP discovery port** | Used to find devices on the LAN/VPN. Same on all your devices. | 7826 |
| **Use exact ports** | If a port is busy, fail instead of quietly using the next one. | On |
| **Tor program** | PC only: full path to `tor.exe` if SRLTCP can't find Tor Browser's. | Empty (auto) |
| **I forward UDP 7828 to this device** | For routers that change ports (OPNsense, pfSense): you forward UDP 7828 here with a static outgoing port (see *Home router: OPNsense / pfSense*). SRLTCP stays on 7828 and offers your public address with it. | Off |
| **I port-forward TCP 7825** | Optional manual setup; direct connections work without it. Off: TCP 7825 accepts connections only from your own networks (private and VPN ranges) and your contacts' known addresses. | Off |

## Ports at a glance

| Port | Protocol | Where | Needs opening? |
|---|---|---|---|
| 7825 | TCP | Each device (peer connections) | Only inside your LAN/VPN firewall, never on the router |
| 7826 | UDP | Each device (LAN/VPN discovery, only when you press Announce) | Same as above |
| 7828 | UDP | Each device (direct connections between networks) | No (on Wi-Fi the app asks the router itself, UPnP / NAT-PMP). It only answers authenticated packets from contacts it's connecting to |
| — | Tor | Each device (onion service) | No. Tor only dials out, and the onion listener is bound to 127.0.0.1 |
| 9876 | TCP | Each device (local web UI, 127.0.0.1 only) | No |

## Privacy summary

- Your IP address is visible to your contacts and the STUN server **only if**
  *direct internet connections* is on, and to the networks between you once
  you're connected directly. Strangers can't learn it from the app: addresses
  travel only inside encrypted sessions with trusted contacts.
- Over Tor (the doorbell), your contact doesn't see your IP address.
- Discovery is manual: only when you press **Announce** (or *Find by IP*) is
  your name and ID sent to the Wi-Fi/LAN and VPN, and only for the next 10
  minutes do you answer others. They're signed, so they can't be faked.
- Discovery never leaves your own networks: nothing is announced on mobile
  data or over the internet.

## What SRLTCP sends by itself (and what security software may report)

SRLTCP only ever talks to **your contacts**, plus Tor and STUN to find them.
Security software at work flagged earlier versions; here is what each kind of
traffic is and what 0.15 changed:

| Traffic | What it is | 0.15 |
|---|---|---|
| Connections to Tor relays ("Tor address blocked") | The doorbell above, to find contacts on other networks | Paused on quiet networks; can be turned off |
| An unknown public IP on UDP 7828 | A contact (or STUN server) during a direct-connection setup | No scanning: only addresses a contact gave; nothing on quiet networks |
| Thousands of UDP probes | The old "birthday punch" | **Removed** |
| Broadcasts on UDP 7826 every 5 s | Old automatic discovery | **Removed**: only when you press Announce |
| UPnP / SSDP to the router | Router port mapping | **Off** by default |
| Dials to a contact that's away | Reconnecting | Stops after 2 minutes until you open their chat, send them something, change networks or restart |

**Reaching contacts.** When a contact can't be reached for 2 minutes, SRLTCP
stops dialing them. It tries again when you open their chat, send them a
message, file or call, when your network changes, or when the app restarts.
You stay reachable the whole time: your listener (same Wi-Fi/VPN) and your
Tor doorbell stay up, so when *they* message or call you, it comes through.

**Who may connect.** TCP 7825 accepts connections only from private
(LAN/VPN), carrier-NAT and link-local addresses, and from contacts' known
addresses (or their IPv6 /64), unless *I port-forward TCP 7825* is on.
Until a connection has proven who it is (the signed handshake), it may send
at most 64 KiB per frame, at most 4 such connections per address and 64 in
all, it must finish within 15 s, strangers get at most 20 handshakes a
minute between them, and a session with someone who isn't a contact is
closed after 5 minutes.

## Home router: OPNsense / pfSense

OPNsense and pfSense give every new connection a **random outgoing port**
("symmetric" or hard NAT), unlike most home routers, which keep the port. A
phone on mobile data is usually behind a hard NAT too, and two hard NATs
can't be crossed directly: the chat stays on **CONNECTING** while you're away
from home. Signs: *Check connection* says this network gives a different port
for every destination, or the log shows a public port that isn't 7828
(`UDP address for direct connections: 68.x.x.x:29503`). Fix it once on the
router, either way:

**A. Static port + port forward (no UPnP):**
1. *Firewall → NAT → Outbound*: mode **Hybrid outbound NAT**, then add a rule:
   Interface **WAN**, TCP/IP version **IPv4**, Protocol **UDP**, Source
   address = the computer running SRLTCP (single host, e.g. `10.0.30.20/32`),
   Source port **7828**, Translation **Interface address**, tick **Static
   port**. Save, Apply. (SRLTCP's public port is now 7828, as STUN shows.)
2. *Firewall → NAT → Port Forward*: add Interface **WAN**, Protocol **UDP**,
   Destination **WAN address**, port **7828** → Redirect target IP = that
   computer, port **7828**, with the associated filter rule. Save, Apply.
3. Give that computer a fixed address (DHCP static mapping). Only SRLTCP's
   encrypted packets are answered on 7828; anything else gets no reply.
4. In SRLTCP on that computer: *Settings → Network → More network options →
   **I forward UDP 7828 to this device***. SRLTCP then stays on port 7828
   (it normally moves to a fresh port after a failed attempt, which would
   leave the forward behind) and gives contacts your public address with
   port 7828, which the router lets in from any port.

**B. UPnP / NAT-PMP:** *System → Firmware → Plugins*: install **os-upnp**;
*Services → Universal Plug and Play*: enable it and NAT-PMP, external
interface WAN, internal interface the computer's LAN/VLAN, and allow only
that computer (an ACL entry like `allow 7828-7828 10.0.30.20/32 7828-7828`).
Then in SRLTCP on that computer: *Settings → Network → Open a port on my
home router* (SRLTCP then also stays on its port).

**IPv6** needs no NAT rules: two devices with IPv6 connect directly through
OPNsense's stateful firewall. If *Check connection* shows IPv6 on both sides
and it still won't connect, add a WAN rule passing **UDP 7828 (IPv6)** to
that computer's IPv6 address.

## Troubleshooting

| Symptom | Check |
|---|---|
| Not sure why a contact won't connect | *Edit contact* → **Check connection**, then **Copy connection report**. |
| Different networks never connect | Tor on and showing "ready" on both devices (Settings → Network), and paired once. |
| A computer or phone at home and a phone on mobile data never connect directly | Turn on **Open a port on my home router** on the device at home (off by default since 0.15). Mobile carriers change ports per destination, and home routers drop packets that don't come from the exact port they expect. |
| Log shows `TCP connect … failed: [WinError 5] Access is denied` | A firewall or security app on this computer blocks SRLTCP (Python) from that address. SRLTCP rests such addresses for 5 minutes instead of retrying. Allow Python in the firewall if the contact is on your network. |
| Header stays on **CONNECTING** | The devices found each other over Tor (so Tor works) but can't connect directly. *Edit contact → Check connection* says why. Most common, in this order: (1) a device at home with **Open a port on my home router** off — turn it on there; (2) a **DNS filter** (AdGuard Home, Pi-hole, NextDNS, the phone's *Private DNS*) blocking the STUN servers — the report says *STUN server names (DNS): blocked*; allow them (below); (3) hard NAT on both carriers — one device on Wi-Fi, or IPv6 on both. |
| Home router is **OPNsense / pfSense** and a phone on mobile data never connects | The router randomizes ports (hard NAT): see *Home router: OPNsense / pfSense* above (static port + port forward, or the UPnP plugin). |
| Using **AdGuard Home** / Pi-hole | Open its *Query log*, search for `stun`; if `stun.l.google.com`, `stun1.l.google.com` or `stun.cloudflare.com` show as *Blocked*, click *Unblock* (or add the allow rules `@@\|\|stun.l.google.com^`, `@@\|\|stun1.l.google.com^`, `@@\|\|stun.cloudflare.com^` under *Filters → Custom filtering rules*). Since 0.16.4 SRLTCP falls back to built-in STUN addresses when DNS blocks them, but allowing them keeps it working if those addresses change. Tor isn't affected by DNS filters. |
| Tor stays at "connecting…" | The network may block Tor. On a PC, check the clock is correct. |
| VPN phone can't reach the home phone | The VPN client's `AllowedIPs` must include the home LAN. |
| Two devices on the VPN from different networks stay on CONNECTING | See *VPN checklist* below. |
| Nothing works after changing ports | Every device must use the same peer and discovery ports. Restart the app. |

## VPN checklist (two VPN clients on different networks)

Two WireGuard clients reach each other through the server, so the server and
both clients must allow client-to-client traffic:

1. **Each client's `AllowedIPs` includes the tunnel subnet** (e.g.
   `10.0.100.0/24`), not only the home LAN. Otherwise WireGuard on that client
   drops packets to the other client's tunnel address. Full tunnel
   (`0.0.0.0/0, ::/0`) includes it.
2. **Firewall rule on the server's WireGuard interface** passing traffic from
   the tunnel subnet to the tunnel subnet (OPNsense: *Firewall → Rules →
   WireGuard (group)*, source and destination the tunnel net, TCP 7825 and
   UDP 7828 at least). Make the rule **IPv4+IPv6** if the tunnel has IPv6
   addresses: an IPv4-only rule makes the live log fill with blocked IPv6
   from the clients.
3. **SRLTCP isn't excluded from the VPN** on the phone (WireGuard app →
   tunnel → *Excluded applications*).
4. **Nothing on either device blocks Python/SRLTCP to the tunnel addresses**
   (Windows Firewall, Malwarebytes). In the log this shows as
   `[WinError 5] Access is denied`.
5. **Find by IP** with the other device's tunnel address if neither has Tor
   (quiet network).

Blocked **IPv6 on UDP/TCP 7828** in the router's log while a contact is away is
SRLTCP trying a direct internet path between the two devices' public IPv6
addresses (only to that contact, a few packets at a time). A home router
drops what arrives before its own device has answered; once a direct path
(the VPN, or the IPv6 one) works, it stops.

