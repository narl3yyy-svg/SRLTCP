"""Simulated USB serial radio modems for two phones (stands in for the Android
``UsbSerialBridge``), close to real hardware:

- **Adapter** (FTDI / CP210x / CDC on the phone): bytes leave at the serial
  line's speed (baud / 10 bytes a second), a write blocks while the adapter's
  small buffer is full, each write costs a USB transfer, and reads return USB
  packets of at most 62 bytes.
- **Radio**: gathers serial bytes into air packets (full, or after a short
  pause in the serial stream), sends them at its *air* rate with a per-packet
  overhead (preamble, sync, header), and has a buffer that overflows when the
  serial side is faster than the air (no flow control: bytes are lost).
- **Air**: ``full`` (both ways at once: a cable, a full-duplex link),
  ``tdm`` (one way at a time, the radio's own MAC takes turns, like SiK
  telemetry radios: no collisions but a turnaround gap), or ``raw`` (one way
  at a time with no MAC, like LoRa serial bridges: two transmissions at once
  destroy each other).
- Air packets are lost at random (``loss``).

Adapters can be plugged in and out while the app runs (``plug`` / ``unplug``).
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field

PORTS = ["usb:0403:6015#0", "usb:303a:1001#0"]


@dataclass
class Radio:
    """One kind of radio modem pair."""

    name: str
    baud: int = 57600
    air_rate: float = 8000.0  # payload bytes a second on the air
    air_overhead: float = 0.004  # seconds per air packet (preamble, sync, header)
    max_packet: int = 252  # payload bytes per air packet
    gather: float = 0.002  # send a partial packet after this much serial silence
    duplex: str = "tdm"  # full | tdm | raw
    turnaround: float = 0.010  # tdm/raw: switching between sending and receiving
    buffer: int = 2048  # radio's serial-to-air buffer (bytes); overflow is lost
    loss: float = 0.02  # air packets lost
    latency: float = 0.005  # extra delay to the other adapter


RADIOS = {
    # A USB serial cable between the phones (null modem): the line is the limit.
    "cable": Radio("cable", air_rate=1e9, air_overhead=0.0, max_packet=64, duplex="full",
                   turnaround=0.0, buffer=1 << 20, loss=0.0, latency=0.001),
    # SiK / 3DR telemetry radios at their defaults: 64 kbps air, its own TDM.
    "sik": Radio("sik", air_rate=7000.0, air_overhead=0.006, max_packet=252, duplex="tdm",
                 turnaround=0.012, buffer=2048, loss=0.02, latency=0.004),
    # SiK with error correction on (halves the air rate), more loss.
    "sik-ecc": Radio("sik-ecc", air_rate=3500.0, air_overhead=0.008, max_packet=252,
                     duplex="tdm", turnaround=0.015, buffer=2048, loss=0.05, latency=0.004),
    # A LoRa serial bridge at a fast setting: no MAC, collisions, long packets.
    "lora": Radio("lora", air_rate=2700.0, air_overhead=0.025, max_packet=240, gather=0.004,
                  duplex="raw", turnaround=0.008, buffer=1000, loss=0.03, latency=0.004),
    # A noisy full-duplex link (ESP-NOW / Bluetooth bridges at the edge of range).
    "noisy": Radio("noisy", air_rate=20000.0, air_overhead=0.002, max_packet=200,
                   duplex="full", turnaround=0.0, buffer=4096, loss=0.10, latency=0.02),
}


@dataclass
class _Side:
    port: str
    plugged: bool = True
    gen: int = 0
    to_radio: list[tuple[float, bytes]] = field(default_factory=list)  # (arrives, bytes)
    serial_free: float = 0.0  # when the adapter's line is free again
    radio_buf: bytearray = field(default_factory=bytearray)
    last_in: float = 0.0  # when the last serial byte reached the radio
    rx: list[tuple[float, bytes]] = field(default_factory=list)  # to the phone
    rx_free: float = 0.0
    on_air_until: float = 0.0
    sending: bytes = b""
    overflow: int = 0
    sent_packets: int = 0
    lost_packets: int = 0
    collided: int = 0


class RadioBridge:
    """Two phones' adapters and radios (the Android UsbSerialBridge API)."""

    def __init__(self, radio: Radio | str = "sik", *, seed: int = 7,
                 write_cost: float = 0.0015, plugged: tuple[bool, bool] = (True, True)) -> None:
        self.radio = RADIOS[radio] if isinstance(radio, str) else radio
        self.byte_time = 10.0 / self.radio.baud
        self.write_cost = write_cost
        self.rnd = random.Random(seed)
        self.sides = {p: _Side(p, plugged=pl) for p, pl in zip(PORTS, plugged, strict=True)}
        self.gens: dict[int, str] = {}
        self.cond = threading.Condition()
        self._turn = 0  # tdm: whose turn is next when both want the air
        self._stop = False
        self._thread = threading.Thread(target=self._air_loop, daemon=True)
        self._thread.start()

    # ── Test controls ──

    def plug(self, port: str) -> None:
        with self.cond:
            self.sides[port].plugged = True

    def unplug(self, port: str) -> None:
        with self.cond:
            side = self.sides[port]
            side.plugged = False
            for gen, p in list(self.gens.items()):
                if p == port:
                    del self.gens[gen]

    def stop(self) -> None:
        self._stop = True

    def stats(self) -> dict[str, dict[str, int]]:
        return {p: {"overflow": s.overflow, "air_packets": s.sent_packets,
                    "lost": s.lost_packets, "collided": s.collided}
                for p, s in self.sides.items()}

    # ── UsbSerialBridge API ──

    def listPorts(self) -> str:  # noqa: N802 - Java names
        return json.dumps([{"device": p, "description": p, "accessible": True, "attached": i}
                           for i, (p, s) in enumerate(self.sides.items()) if s.plugged])

    def requestPermission(self, port: str) -> bool:  # noqa: N802
        return True

    def open(self, port: str, baud: int, ask: bool) -> int:
        with self.cond:
            side = self.sides.get(port)
            if side is None or not side.plugged:
                return -2
            gen = max(self.gens, default=0) + 1
            self.gens[gen] = port
            side.gen = gen
            return gen

    def getLastError(self) -> str:  # noqa: N802
        return ""

    def isOpen(self, gen: int) -> bool:  # noqa: N802
        return gen in self.gens

    def close(self, gen: int) -> None:
        with self.cond:
            self.gens.pop(gen, None)

    def write(self, gen: int, data: bytes, timeout_ms: int) -> None:
        with self.cond:
            port = self.gens.get(gen)
            if port is None:
                raise OSError("device gone")
            side = self.sides[port]
        time.sleep(self.write_cost)  # (one USB transfer)
        with self.cond:
            now = time.monotonic()
            start = max(now, side.serial_free)
            side.serial_free = start + len(data) * self.byte_time
            side.to_radio.append((side.serial_free, bytes(data)))
            done = side.serial_free
            self.cond.notify_all()
        wait = done - time.monotonic() - 256 * self.byte_time  # (adapter buffer)
        if wait > 0:
            time.sleep(wait)

    def read(self, gen: int, size: int, timeout_ms: int) -> bytes:
        deadline = time.monotonic() + timeout_ms / 1000
        with self.cond:
            while True:
                port = self.gens.get(gen)
                if port is None:
                    raise OSError("device gone")
                ready = self.sides[port].rx
                now = time.monotonic()
                if ready and ready[0][0] <= now:
                    out = bytearray()
                    while ready and ready[0][0] <= now and len(out) < 62:
                        at, chunk = ready[0]
                        take = chunk[: 62 - len(out)]
                        out += take
                        if len(take) == len(chunk):
                            ready.pop(0)
                        else:
                            ready[0] = (at, chunk[len(take):])
                    return bytes(out)
                if now >= deadline:
                    return b""
                wake = deadline if not ready else min(deadline, ready[0][0])
                self.cond.wait(max(0.0005, wake - now))

    # ── The air ──

    def _air_loop(self) -> None:
        r = self.radio
        a, b = self.sides.values()
        while not self._stop:
            with self.cond:
                now = time.monotonic()
                for side in (a, b):
                    self._serial_in(side, now)
                for side, other in ((a, b), (b, a)):
                    if side.sending and now >= side.on_air_until:
                        self._land(side, other, now)
                for side, other in self._order(a, b):
                    if side.sending or not side.radio_buf:
                        continue
                    full = len(side.radio_buf) >= r.max_packet
                    if not full and now - side.last_in < r.gather:
                        continue
                    if r.duplex == "tdm" and (other.sending or now < other.on_air_until
                                              + r.turnaround):
                        continue  # the radio's MAC waits for the other side's slot
                    payload = bytes(side.radio_buf[: r.max_packet])
                    del side.radio_buf[: r.max_packet]
                    side.sending = payload
                    side.on_air_until = now + r.air_overhead + len(payload) / r.air_rate
                    side.sent_packets += 1
                    if r.duplex == "raw" and other.sending:
                        side.collided += 1
                        other.collided += 1
                        side.sending = other.sending = b"\0"  # both lost
                self.cond.notify_all()
            time.sleep(0.0005)

    def _order(self, a: _Side, b: _Side) -> list[tuple[_Side, _Side]]:
        self._turn ^= 1  # (fair when both want the air)
        return [(a, b), (b, a)] if self._turn else [(b, a), (a, b)]

    def _serial_in(self, side: _Side, now: float) -> None:
        while side.to_radio and side.to_radio[0][0] <= now:
            _at, data = side.to_radio.pop(0)
            room = self.radio.buffer - len(side.radio_buf)
            if room < len(data):
                side.overflow += len(data) - max(room, 0)
            side.radio_buf += data[: max(room, 0)]
            side.last_in = now

    def _land(self, side: _Side, other: _Side, now: float) -> None:
        payload, side.sending = side.sending, b""
        if payload == b"\0" or not other.plugged:
            return
        if self.rnd.random() < self.radio.loss:
            side.lost_packets += 1
            return
        arrive = max(now + self.radio.latency, other.rx_free)
        arrive += len(payload) * self.byte_time  # into the phone at the line's speed
        other.rx_free = arrive
        other.rx.append((arrive, payload))
