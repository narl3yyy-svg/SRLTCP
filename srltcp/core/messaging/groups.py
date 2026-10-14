"""Group chats over the existing pairwise E2EE sessions.

There is no group server and no group key. A group is a member list kept by
its creator (the *owner*); every message travels over the normal one-to-one
encrypted sessions:

* The author signs each message with their identity key and sends it to the
  owner and to every member they have as a contact.
* The owner forwards it to the remaining members, so members who are not
  each other's contacts still talk. Forwarded messages keep the author's
  signature, so the owner cannot put words in anyone's mouth.
* Recipients drop duplicates by message id.

Only the owner changes the member list (``GROUP_UPDATE``, versioned); members
can leave (``GROUP_LEAVE``). A new group is accepted only from a trusted
contact, and group traffic is accepted only from current members.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from srltcp.core.messaging.models import ChatMessage
from srltcp.core.protocol.crypto import sign_bytes, verify_bytes
from srltcp.core.protocol.messages import MessageType, decode_payload, encode_payload
from srltcp.core.protocol.signed import verify_identity_binding
from srltcp.utils.jsonstore import load_json, save_json
from srltcp.utils.logging import get_logger
from srltcp.utils.platform import data_dir

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend
    from srltcp.core.messaging.links import PeerLink

log = get_logger(__name__)

GROUP_MSG_TYPE = "group_text"
MAX_GROUP_MEMBERS = 32
MAX_GROUP_NAME = 64
MAX_GROUP_TEXT = 16384
_HEX = set("0123456789abcdef")


class GroupError(ValueError):
    pass


def is_valid_group_id(group_id: str) -> bool:
    return len(group_id) == 32 and set(group_id) <= _HEX


def _canonical(fields: dict[str, Any]) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _signed_fields(group_id: str, msg_id: str, sender: str, text: str, ts: float) -> bytes:
    return _canonical({"g": group_id, "i": msg_id, "s": sender, "t": text, "ts": ts})


class GroupStore:
    """``groups.json`` in the data dir (owner-only permissions)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "groups.json")
        self._groups: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        raw = load_json(self.path, {})
        for item in raw.get("groups", []) if isinstance(raw, dict) else []:
            gid = str(item.get("id", ""))
            if is_valid_group_id(gid):
                self._groups[gid] = item

    def save(self) -> None:
        save_json(self.path, {"groups": list(self._groups.values())})

    def get(self, group_id: str) -> dict[str, Any] | None:
        return self._groups.get(group_id)

    def put(self, group: dict[str, Any]) -> None:
        self._groups[group["id"]] = group
        self.save()

    def remove(self, group_id: str) -> bool:
        if self._groups.pop(group_id, None) is None:
            return False
        self.save()
        return True

    def all(self) -> list[dict[str, Any]]:
        return list(self._groups.values())


def member_hashes(group: dict[str, Any]) -> list[str]:
    return [str(m.get("hash_id", "")) for m in group.get("members", [])]


def member_key(group: dict[str, Any], hash_id: str) -> str:
    for m in group.get("members", []):
        if m.get("hash_id") == hash_id:
            return str(m.get("public_key", ""))
    return ""


class GroupMixin:
    """Group chat support for :class:`MessagingBackend`."""

    groups: GroupStore

    def _init_groups(self: MessagingBackend) -> None:
        self.groups = GroupStore()

    def _me(self: MessagingBackend) -> str:
        return self.identities["tcp"].hash_id

    # ── Views ──

    def list_groups(self: MessagingBackend) -> list[dict[str, Any]]:
        me = self._me()
        out = []
        for g in sorted(self.groups.all(), key=lambda g: str(g.get("name", "")).lower()):
            members = []
            for m in g.get("members", []):
                h = str(m.get("hash_id", ""))
                contact = self.trusted.get(h)
                members.append(
                    {
                        "hash_id": h,
                        "name": "You"
                        if h == me
                        else (contact.name if contact else m.get("name", h[:8])),
                        "is_contact": bool(contact and not contact.blocked),
                        "online": bool((link := self.get_link(h)) and link.handshake_complete),
                    }
                )
            out.append(
                {
                    "id": g["id"],
                    "name": g.get("name", "Group"),
                    "owner": g.get("owner", ""),
                    "is_owner": g.get("owner") == me,
                    "members": members,
                    "left": bool(g.get("left")),
                    "version": int(g.get("version", 0)),
                    "created_at": g.get("created_at", 0),
                }
            )
        return out

    def member_name(self: MessagingBackend, group_id: str, hash_id: str) -> str:
        contact = self.trusted.get(hash_id)
        if contact:
            return contact.name
        for m in (self.groups.get(group_id) or {}).get("members", []):
            if m.get("hash_id") == hash_id:
                return str(m.get("name") or hash_id[:8])
        return hash_id[:8]

    def get_group_view(self: MessagingBackend, group_id: str) -> dict[str, Any] | None:
        return next((g for g in self.list_groups() if g["id"] == group_id), None)

    # ── Owner actions ──

    def _member_entry(self: MessagingBackend, hash_id: str) -> dict[str, str]:
        if hash_id == self._me():
            ident = self.identities["tcp"]
            return {
                "hash_id": hash_id,
                "name": ident.name,
                "public_key": ident.public_bytes().hex(),
            }
        peer = self.trusted.get(hash_id)
        if not peer or peer.blocked:
            raise GroupError("members must be trusted contacts")
        key = peer.public_key
        if not key:
            link = self.get_link(hash_id)
            if link and link.public_key:
                key = link.public_key.hex()
                self.trusted.set_public_key(hash_id, key)
        if not key or verify_identity_binding(hash_id, key) is None:
            raise GroupError(f"connect to {peer.name} once before adding them to a group")
        return {"hash_id": hash_id, "name": peer.name, "public_key": key}

    def _clean_name(self: MessagingBackend, name: str) -> str:
        name = " ".join(str(name or "").split())[:MAX_GROUP_NAME]
        if not name:
            raise GroupError("group name required")
        return name

    async def create_group(self: MessagingBackend, name: str, members: list[str]) -> dict[str, Any]:
        me = self._me()
        wanted = [h.lower() for h in dict.fromkeys(members) if h and h.lower() != me]
        if not wanted:
            raise GroupError("pick at least one contact")
        if len(wanted) + 1 > MAX_GROUP_MEMBERS:
            raise GroupError(f"groups are limited to {MAX_GROUP_MEMBERS} members")
        group: dict[str, Any] = {
            "id": secrets.token_hex(16),
            "name": self._clean_name(name),
            "owner": me,
            "members": [self._member_entry(me)] + [self._member_entry(h) for h in wanted],
            "version": 1,
            "created_at": time.time(),
        }
        self.groups.put(group)
        log.info("Created group %s with %d members", group["name"], len(group["members"]))
        await self._broadcast_group(group, member_hashes(group))
        await self._emit_group(group)
        return self.get_group_view(group["id"]) or {}

    async def update_group(
        self: MessagingBackend,
        group_id: str,
        *,
        name: str | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        group = self._owned_group(group_id)
        me = self._me()
        old_members = member_hashes(group)
        if name is not None:
            group["name"] = self._clean_name(name)
        members = list(group["members"])
        for h in add or []:
            h = h.lower()
            if h != me and h not in member_hashes({"members": members}):
                members.append(self._member_entry(h))
        drop = {h.lower() for h in remove or []} - {me}
        members = [m for m in members if m.get("hash_id") not in drop]
        if len(members) > MAX_GROUP_MEMBERS:
            raise GroupError(f"groups are limited to {MAX_GROUP_MEMBERS} members")
        group["members"] = members
        group["version"] = int(group.get("version", 0)) + 1
        self.groups.put(group)
        # Removed members get the new list too, so they know they are out.
        await self._broadcast_group(group, list(dict.fromkeys(old_members + member_hashes(group))))
        await self._emit_group(group)
        return self.get_group_view(group_id) or {}

    def _owned_group(self: MessagingBackend, group_id: str) -> dict[str, Any]:
        group = self.groups.get(group_id)
        if not group:
            raise GroupError("group not found")
        if group.get("owner") != self._me():
            raise GroupError("only the group's creator can change it")
        if group.get("left"):
            raise GroupError("group was closed")
        return group

    async def leave_group(self: MessagingBackend, group_id: str) -> None:
        group = self.groups.get(group_id)
        if not group:
            raise GroupError("group not found")
        me = self._me()
        if group.get("owner") == me and not group.get("left"):
            # The creator leaving closes the group for everyone.
            everyone = member_hashes(group)
            group["members"] = [m for m in group["members"] if m.get("hash_id") == me]
            group["version"] = int(group.get("version", 0)) + 1
            group["left"] = True
            self.groups.put(group)
            await self._broadcast_group(group, everyone)
        elif not group.get("left"):
            group["left"] = True
            self.groups.put(group)
            owner = str(group.get("owner", ""))
            await self._send_group_packet(
                owner, MessageType.GROUP_LEAVE, {"g": group_id}, queue_kind="leave"
            )
        await self._emit_group(group)

    async def delete_group(self: MessagingBackend, group_id: str) -> bool:
        group = self.groups.get(group_id)
        if not group:
            return False
        if not group.get("left"):
            await self.leave_group(group_id)
        self.groups.remove(group_id)
        before = len(self._messages)
        self._messages = [m for m in self._messages if m.metadata.get("group_id") != group_id]
        if len(self._messages) != before:
            self._persist_messages()
        return True

    # ── Sending ──

    def _group_targets(
        self: MessagingBackend, group: dict[str, Any], exclude: set[str]
    ) -> list[str]:
        """Who we send a group message to directly."""
        me = self._me()
        owner = str(group.get("owner", ""))
        out = []
        for h in member_hashes(group):
            if h == me or h in exclude:
                continue
            if me == owner or h == owner or self.is_trusted(h):
                out.append(h)
        return out

    async def send_group_message(self: MessagingBackend, group_id: str, text: str) -> ChatMessage:
        group = self.groups.get(group_id)
        me = self._me()
        if not group or group.get("left") or me not in member_hashes(group):
            raise GroupError("you are not in this group")
        text = str(text)[:MAX_GROUP_TEXT]
        if not text.strip():
            raise GroupError("empty message")
        msg = ChatMessage.create(
            sender_hash=me,
            recipient_hash=group_id,
            text=text,
            transport="tcp",
            msg_type=GROUP_MSG_TYPE,
        )
        sig = sign_bytes(
            self.identities["tcp"].private_key,
            _signed_fields(group_id, msg.id, me, text, msg.timestamp),
        ).hex()
        msg.metadata = {
            "group_id": group_id,
            "sig": sig,
            "pending_to": self._group_targets(group, set()),
            "delivered_to": [],
        }
        msg.status = "pending"
        self._messages.append(msg)
        self._persist_messages()
        await self._push_group_message(msg)
        if self._on_message:
            await self._on_message(msg.to_dict())
        return msg

    def _group_wire(self: MessagingBackend, msg: ChatMessage) -> dict[str, Any]:
        return {
            "g": msg.metadata.get("group_id"),
            "i": msg.id,
            "s": msg.sender_hash,
            "t": msg.text,
            "ts": msg.metadata.get("ts", msg.timestamp),  # the signed (author's) time
            "sig": msg.metadata.get("sig", ""),
        }

    async def _push_group_message(self: MessagingBackend, msg: ChatMessage) -> None:
        """Send to every member still owed this message that we can reach now."""
        sent_any = False
        for h in list(msg.metadata.get("pending_to", [])):
            if await self._send_group_packet(h, MessageType.GROUP_TEXT, self._group_wire(msg)):
                sent_any = True
        if sent_any and msg.status == "pending":
            msg.status = "sent"
            self._persist_messages()

    async def _send_group_packet(
        self: MessagingBackend,
        hash_id: str,
        msg_type: MessageType,
        payload: dict[str, Any],
        *,
        queue_kind: str = "",
    ) -> bool:
        link = self.get_link(hash_id)
        if not link or not link.handshake_complete or not self._is_link_reachable(link):
            if queue_kind:
                self._queue_group_control(hash_id, queue_kind, payload)
            if self.is_trusted(hash_id):
                self._kick_connect(hash_id)
            return False
        try:
            packet = self._seal(link, msg_type, encode_payload(payload))
            await self._send_raw(link.transport_peer_id, link.transport, packet)
        except (KeyError, OSError, RuntimeError, ConnectionError) as exc:
            log.info("Group send to %s failed, will retry: %s", hash_id[:8], exc)
            if queue_kind:
                self._queue_group_control(hash_id, queue_kind, payload)
            return False
        return True

    def _queue_group_control(
        self: MessagingBackend, hash_id: str, kind: str, payload: dict[str, Any]
    ) -> None:
        queue = self.__dict__.setdefault("_group_control_queue", {})
        queue.setdefault(hash_id, {})[f"{kind}:{payload.get('g')}"] = payload

    async def _broadcast_group(
        self: MessagingBackend, group: dict[str, Any], to: list[str]
    ) -> None:
        """Send the member list; whoever is offline gets it on their next link-up."""
        me = self._me()
        owed = set(group.get("sync_to", [])) | {h for h in to if h != me}
        for h in sorted(owed):
            if await self._send_group_update(group, h):
                owed.discard(h)
        group["sync_to"] = sorted(owed)
        self.groups.put(group)

    async def _send_group_update(
        self: MessagingBackend, group: dict[str, Any], hash_id: str
    ) -> bool:
        payload: dict[str, Any] = {
            "group": {k: group[k] for k in ("id", "name", "owner", "members", "version")}
        }
        if group.get("left"):
            payload["group"]["closed"] = True
        return await self._send_group_packet(hash_id, MessageType.GROUP_UPDATE, payload)

    async def sync_groups_with(self: MessagingBackend, hash_id: str) -> None:
        """On link-up: queued leaves, the latest member list of groups we run,
        and group messages this peer has not confirmed yet."""
        queue = self.__dict__.get("_group_control_queue", {})
        for payload in list(queue.pop(hash_id, {}).values()):
            await self._send_group_packet(
                hash_id, MessageType.GROUP_LEAVE, payload, queue_kind="leave"
            )
        me = self._me()
        for group in self.groups.all():
            if group.get("owner") != me or hash_id == me:
                continue
            owed = group.get("sync_to", [])
            if hash_id not in member_hashes(group) and hash_id not in owed:
                continue
            if await self._send_group_update(group, hash_id) and hash_id in owed:
                owed.remove(hash_id)
                self.groups.put(group)
        for msg in self._messages:
            if msg.msg_type != GROUP_MSG_TYPE or hash_id not in msg.metadata.get("pending_to", []):
                continue
            wire = self._group_wire(msg)
            sent = await self._send_group_packet(hash_id, MessageType.GROUP_TEXT, wire)
            if sent and msg.status == "pending":
                msg.status = "sent"
                self._persist_messages()
                if self._on_message:
                    await self._on_message(msg.to_dict())

    def peers_owed_group_messages(self: MessagingBackend) -> set[str]:
        owed: set[str] = set()
        for msg in self._messages:
            if msg.msg_type == GROUP_MSG_TYPE:
                owed.update(msg.metadata.get("pending_to", []))
        return owed

    async def _group_receipt(self: MessagingBackend, hash_id: str, msg_id: str) -> bool:
        """A member confirmed a group message. Returns True if it was one."""
        for msg in reversed(self._messages):
            if msg.id != msg_id or msg.msg_type != GROUP_MSG_TYPE:
                continue
            pending = msg.metadata.get("pending_to", [])
            if hash_id not in pending:
                return True
            pending.remove(hash_id)
            delivered = msg.metadata.setdefault("delivered_to", [])
            if hash_id not in delivered:
                delivered.append(hash_id)
            self._persist_messages()
            if msg.sender_hash == self._me():
                msg.status = "delivered" if not pending else "sent"
                self._persist_messages()
                if self._on_message:
                    await self._on_message(msg.to_dict())
            return True
        return False

    # ── Receiving ──

    async def _handle_group_packet(
        self: MessagingBackend, link: PeerLink, msg_type: MessageType, body: bytes
    ) -> None:
        data = decode_payload(body)
        if msg_type == MessageType.GROUP_UPDATE:
            await self._handle_group_update(link, data)
        elif msg_type == MessageType.GROUP_TEXT:
            await self._handle_group_text(link, data)
        elif msg_type == MessageType.GROUP_LEAVE:
            await self._handle_group_leave(link, data)

    async def _handle_group_update(
        self: MessagingBackend, link: PeerLink, data: dict[str, Any]
    ) -> None:
        incoming = data.get("group")
        if not isinstance(incoming, dict):
            return
        gid = str(incoming.get("id", ""))
        sender = link.hash_id
        if not is_valid_group_id(gid) or incoming.get("owner") != sender:
            return  # only the owner, speaking for itself, changes a group
        existing = self.groups.get(gid)
        if existing:
            if existing.get("owner") != sender:
                return
            if int(incoming.get("version", 0)) <= int(existing.get("version", 0)):
                return
        elif not self.is_trusted(sender):
            log.info("Ignoring group invite from non-contact %s", sender[:8])
            return
        members = []
        for m in incoming.get("members", [])[:MAX_GROUP_MEMBERS]:
            if not isinstance(m, dict):
                continue
            h = str(m.get("hash_id", "")).lower()
            key = str(m.get("public_key", "")).lower()
            if verify_identity_binding(h, key) is None:
                continue
            members.append(
                {"hash_id": h, "name": str(m.get("name", ""))[:64] or h[:8], "public_key": key}
            )
        me = self._me()
        group: dict[str, Any] = {
            "id": gid,
            "name": str(incoming.get("name", "Group"))[:MAX_GROUP_NAME] or "Group",
            "owner": sender,
            "members": members,
            "version": int(incoming.get("version", 1)),
            "created_at": existing.get("created_at", time.time()) if existing else time.time(),
        }
        if incoming.get("closed") or me not in member_hashes(group):
            if not existing:
                return  # an invite that doesn't include us
            group["left"] = True
        self.groups.put(group)
        log.info("Group %s updated by %s (v%s)", group["name"], sender[:8], group["version"])
        await self._emit_group(group)

    async def _handle_group_text(
        self: MessagingBackend, link: PeerLink, data: dict[str, Any]
    ) -> None:
        gid = str(data.get("g", ""))
        msg_id = str(data.get("i", ""))[:64]
        author = str(data.get("s", ""))
        text = str(data.get("t", ""))[:MAX_GROUP_TEXT]
        ts = data.get("ts")
        group = self.groups.get(gid)
        relay = link.hash_id
        if not group or group.get("left") or not msg_id or not isinstance(ts, int | float):
            return
        members = member_hashes(group)
        if relay not in members or author not in members:
            return
        if relay != author and relay != group.get("owner"):
            return  # only the author or the owner (forwarding) may deliver it
        key_hex = member_key(group, author)
        pub = verify_identity_binding(author, key_hex)
        try:
            sig = bytes.fromhex(str(data.get("sig", "")))
        except ValueError:
            return
        if pub is None or not verify_bytes(
            pub, sig, _signed_fields(gid, msg_id, author, text, float(ts))
        ):
            log.warning("Bad signature on group message from %s via %s", author[:8], relay[:8])
            return
        await self._send_receipt(link, msg_id, "delivered")
        if any(m.id == msg_id and m.metadata.get("group_id") == gid for m in self._messages):
            return
        me = self._me()
        msg = ChatMessage(
            id=msg_id,
            sender_hash=author,
            recipient_hash=gid,
            text=text,
            transport=link.transport,
            status="delivered",
            msg_type=GROUP_MSG_TYPE,
            metadata={"group_id": gid, "sig": data.get("sig", ""), "via": relay, "ts": ts},
        )
        if abs(time.time() - float(ts)) < 7 * 86400:
            msg.metadata["sent_at"] = float(ts)
        if group.get("owner") == me:
            # Forward to everyone the author may not reach directly.
            msg.metadata["pending_to"] = [h for h in members if h not in (me, author, relay)]
            msg.metadata["delivered_to"] = []
        self._messages.append(msg)
        self._persist_messages()
        if msg.metadata.get("pending_to"):
            await self._push_group_message(msg)
        if self._on_message:
            await self._on_message({**msg.to_dict(), "_new": True})

    async def _handle_group_leave(
        self: MessagingBackend, link: PeerLink, data: dict[str, Any]
    ) -> None:
        gid = str(data.get("g", ""))
        group = self.groups.get(gid)
        if not group or group.get("owner") != self._me() or group.get("left"):
            return
        if link.hash_id not in member_hashes(group):
            return
        log.info("%s left group %s", link.hash_id[:8], group.get("name"))
        group["members"] = [m for m in group["members"] if m.get("hash_id") != link.hash_id]
        group["version"] = int(group.get("version", 0)) + 1
        self.groups.put(group)
        await self._broadcast_group(group, member_hashes(group) + [link.hash_id])
        await self._emit_group(group)

    async def _emit_group(self: MessagingBackend, group: dict[str, Any]) -> None:
        if self._on_event:
            view = self.get_group_view(group["id"])
            await self._on_event({"kind": "group_update", "group": view})
