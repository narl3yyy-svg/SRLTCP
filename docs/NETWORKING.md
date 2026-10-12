# Networking guide

This page explains how SRLTCP connects two devices and what each setting under
**Settings → Network** does. Short version: **leave the defaults**. The app
picks the best path by itself and switches when your network changes.

## How SRLTCP finds a path

For each contact, the app tries these in order and uses the first that works:

| # | Path | When it works | What sees your traffic |
|---|---|---|---|
| 1 | **Direct, same network** | Both on the same Wi-Fi, or both on the same VPN (WireGuard) | Only the two devices |
| 2 | **Direct, across networks** | One on the VPN and the other on the home Wi-Fi; or two different home networks (hole punching, see below) | Only the two devices |
| 3 | **Internet relay** | Always, as long as both devices can reach your relay | The relay forwards sealed packets it can't read |

Every path carries the same end-to-end encrypted session (signed handshake,
forward secrecy). A path is only a pipe; changing paths never weakens
encryption. While on the relay, the app keeps trying to get back to a direct
path, every 45 seconds per contact, and moves over as soon as one works.

The badge in the chat header shows which path a contact is on: **DIRECT**
or **RELAY**.

## Common setups

**Both phones on the same Wi-Fi or the same VPN.** Works out of the box. Pair
once (QR, code or NFC) and you're done.

**One phone on the VPN, the other on the home Wi-Fi.** Works directly, if the
VPN can reach the home network. For a home WireGuard server, that means
the phone's `AllowedIPs` include the home LAN, for example
`10.0.100.0/24, 192.168.1.0/24`. The two phones tell each other their
addresses (LAN and VPN) inside the encrypted session, so neither has to type
anything. If the VPN can't reach the LAN, they use the relay.

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

## Hole punching (direct internet connections)

Home routers block incoming connections, but they let replies to *outgoing*
connections back in. If both phones dial each other at the same moment, each
router sees the other phone's packets as replies, and a direct connection
opens. Nothing on either router needs to be configured.

How it works in SRLTCP:
1. Each phone connects to the relay from port **7828** (peer port + 3). The
   relay tells the phone which public address and port the internet sees.
2. The phones exchange those addresses **inside the encrypted session**, so
   only your contacts ever see them, never the relay or anyone else.
3. Both phones dial each other at an agreed moment. The first connection that
   opens is checked with the normal signed handshake (an impostor can't take
   it over) and replaces the relay session.

When it doesn't work, the relay simply keeps carrying the traffic:
- **Carrier-grade NAT** on mobile data usually picks a new random port for
  every destination ("strict" or "symmetric" NAT). Hole punching can't
  predict that port.
- **Some corporate or guest networks** block it.
- **Turned off.** Turning off *Direct internet connections* stops it, so
  contacts never learn your IP address (relay only).

**IPv6.** If both phones have global IPv6 addresses, the app tries those too.
Many networks let IPv6 through much more easily than IPv4 NAT.

## Settings, one by one

### Over the internet

| Setting | What it does | Default |
|---|---|---|
| **Use the internet when there's no LAN/VPN path** | Uses the relay when there's no direct path. Off means LAN/VPN only. | On |
| **Relay code** | `srltcp-relay://host:port#…`, printed by `srltcp relay`. It contains the server address, the certificate fingerprint (so nobody can impersonate the relay) and the access key. Paste it on one phone; contacts receive it inside the encrypted session. Keep it private. | Empty |
| **Try direct internet connections** | Hole punching, described above. Shares your public IP address **only with your contacts**, inside the encrypted session. Off: relay only, contacts never see your IP. | On |
| **Status** | Which relays you're connected to, how many contacts use each one, and your public address as the relay sees it. This is shown only to you. | — |

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
| **Handshake protocol** | *Identity* is the default and is tested. *Noise XX* is experimental, and both sides must match. | Identity |
| **External call relay (STUN/TURN)** | Not needed: calls travel inside the SRLTCP connection. This is only a fallback for unusual networks. | Empty |
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
| 7828 | TCP | Each device (hole punching, outgoing) | No, it only dials out |
| 9876 | TCP | Each device (local web UI, 127.0.0.1 only) | No |

## Privacy summary

- Your IP address is visible to: the relay operator (you, if you run it), and
  your contacts **only if** *direct internet connections* is on. Strangers
  can't learn it from the app, because addresses travel only inside encrypted
  sessions with trusted contacts.
- The relay never sees names, identity keys, message types or content, or
  which of your contacts you're talking to. It sees IP addresses, timing and
  amounts of data.
- On the LAN/VPN, discovery announcements (your name and ID) are visible to
  devices on that network. They're signed, so they can't be faked.

## Troubleshooting

| Symptom | Check |
|---|---|
| Contact always shows **relay**, never direct | Normal on mobile data (carrier NAT). On home networks, make sure *Direct internet connections* is on for both phones. Some routers block hole punching. |
| VPN phone can't reach the home phone directly | The VPN client's `AllowedIPs` must include the home LAN, and the WireGuard server must route or NAT to the LAN. |
| "Relay unreachable" | Is the server up, TCP 7827 open on its firewall, and the code pasted correctly? |
| "Relay certificate doesn't match" | The code is from a different relay, the relay's certificate was regenerated, or something is intercepting the connection. Get a fresh code from `srltcp relay`. |
| Nothing works after changing ports | Every device must use the same peer and discovery ports. Restart the app after changing them. |
