#!/usr/bin/env python3
"""Simulate SRLTCP users end-to-end on one machine.

Starts three real nodes (alice, bob, carol) with separate data directories,
then drives their web UIs in Chromium the way people would:

  1. Alice shows a pairing code, Bob pastes it -> both trust each other
  2. Messages with delivery receipts, an image transfer with preview
  3. A WebRTC video call (fake camera/mic) and a declined voice call
  4. Phone-sized layout: Carol discovers Alice over UDP broadcast,
     sends a contact request, Alice accepts, queued message arrives
  5. Carol goes offline; Alice's message is delivered after Carol restarts

Requirements: ``pip install playwright`` and a Chromium (``playwright install
chromium`` or set CHROMIUM=/path/to/chrome).

Usage:  python scripts/simulate.py [--out DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
LAUNCH: dict = {"args": ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]}
if os.environ.get("CHROMIUM"):
    LAUNCH["executable_path"] = os.environ["CHROMIUM"]
OUT = Path(".")
errors: list[str] = []


class Node:
    def __init__(self, name: str, web: int, tcp: int, disc: int, base: Path) -> None:
        self.name, self.web, self.tcp, self.disc = name, web, tcp, disc
        self.data = base / name
        self.data.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = {**os.environ, "SRLTCP_DATA_DIR": str(self.data)}
        log = open(self.data / "node.log", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "srltcp", "web", "--name", self.name,
             "--port", str(self.web), "--tcp-port", str(self.tcp),
             "--discovery-port", str(self.disc)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )

    async def wait_ready(self, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.web), timeout=0.5):
                    if (self.data / "web_token").exists():
                        return
            except OSError:
                pass
            await asyncio.sleep(0.3)
        raise RuntimeError(f"{self.name} did not start (see {self.data}/node.log)")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def url(self) -> str:
        token = (self.data / "web_token").read_text().strip()
        return f"https://127.0.0.1:{self.web}/?token={token}"


NODES: dict[str, Node] = {}


async def open_ui(browser, name: str, mobile: bool = False):  # type: ignore[no-untyped-def]
    vp = {"width": 390, "height": 844} if mobile else {"width": 1280, "height": 800}
    ctx = await browser.new_context(
        ignore_https_errors=True, bypass_csp=True, viewport=vp, is_mobile=mobile,
        has_touch=mobile, permissions=["clipboard-read", "clipboard-write", "camera", "microphone"],
    )
    page = await ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"[{name}] pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"[{name}] console: {m.text}"))
    await page.goto(NODES[name].url())
    await page.wait_for_load_state("networkidle")
    if mobile:  # what the Android app does after the page loads
        await page.evaluate("document.documentElement.classList.add('android-app');"
                            "window.applyMobileLayout && window.applyMobileLayout('android')")
    if await page.is_visible("#setup-overlay"):
        await page.fill("#setup-name", name.capitalize())
        await page.click("#setup-form button[type=submit]")
        await page.wait_for_selector("#setup-overlay", state="hidden", timeout=10000)
    return page


async def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    await page.screenshot(path=str(OUT / f"{name}.png"))


async def desktop_flow() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH)
        alice = await open_ui(browser, "alice")
        bob = await open_ui(browser, "bob")
        print("setup done")

        # --- Pairing: Alice shows her code, Bob pastes the link ---
        await alice.click("#btn-pair")
        await alice.wait_for_selector("#pair-qr img", timeout=10000)
        await shot(alice, "01-alice-pair-qr")
        await alice.click("#pair-copy")
        link = await alice.evaluate("navigator.clipboard.readText()")
        assert link.startswith("srltcp://pair#"), link
        print("pair link length", len(link))
        await bob.click("#btn-pair")
        await bob.fill("#pair-code-input", link)
        await bob.click("#pair-import")
        await bob.wait_for_selector("#chat-active:not(.hidden)", timeout=10000)
        print("bob paired + chat open")
        await alice.click("#pair-close")
        # Alice should auto-trust Bob (mutual pairing)
        await alice.click(".peer-tab[data-tab=trusted]")
        await alice.wait_for_selector("#contacts .contact[data-name='Bob']", timeout=15000)
        print("alice sees bob as trusted contact")

        # --- Messaging ---
        await bob.wait_for_function("!document.querySelector('#msg-input').disabled")
        await bob.fill("#msg-input", "Hi Alice! 👋 testing SRLTCP")
        await bob.click("#send-btn")
        await alice.click("#contacts .contact[data-name='Bob']")
        await alice.wait_for_selector("text=Hi Alice! 👋 testing SRLTCP", timeout=10000)
        print("alice received message")
        await bob.wait_for_selector(".bubble.out .bubble-status.delivered", timeout=10000)
        print("bob sees delivered receipt")
        await alice.fill("#msg-input", "Hey Bob, got it loud and clear")
        await alice.press("#msg-input", "Enter")
        await bob.wait_for_selector("text=Hey Bob, got it loud and clear", timeout=10000)
        print("bob received reply")
        await shot(bob, "02-bob-chat")

        # --- File transfer ---
        f = str(OUT / "test-photo.png")
        await alice.screenshot(path=f)  # any image will do
        await bob.set_input_files("#file-input", f)
        await alice.wait_for_selector(".bubble.in img.chat-image", timeout=20000)
        print("alice received image preview")
        await shot(alice, "03-alice-received-image")

        # --- Video call ---
        await bob.click("#btn-call-video")
        await alice.wait_for_selector("#call-overlay:not(.hidden)[data-mode=incoming]", timeout=10000)
        await shot(alice, "04-alice-incoming-call")
        await alice.click("#call-accept-video")
        await alice.wait_for_function("document.querySelector('#call-status').textContent.includes('encrypted')", timeout=20000)
        await bob.wait_for_function("document.querySelector('#call-status').textContent.includes('encrypted')", timeout=20000)
        await asyncio.sleep(2)
        vw = await alice.evaluate("document.querySelector('#call-remote-video').videoWidth")
        print("call connected; alice receives remote video width", vw)
        await shot(alice, "05-alice-in-call")
        await bob.click("#call-hangup")
        await alice.wait_for_selector("#call-overlay", state="hidden", timeout=10000)
        print("call ended on both sides")

        # --- Voice call declined ---
        await alice.click("#btn-call-voice")
        await bob.wait_for_selector("#call-overlay:not(.hidden)[data-mode=incoming]", timeout=10000)
        await bob.click("#call-decline")
        await alice.wait_for_selector("#call-overlay", state="hidden", timeout=10000)
        print("declined call handled")

        await browser.close()


async def mobile_flow() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH)
        carol = await open_ui(browser, "carol", mobile=True)
        alice = await open_ui(browser, "alice")
        await carol.screenshot(path=f"{OUT}/10-carol-mobile-home.png")

        # LAN discovery: Alice announces, Carol sees her in Discovered
        await alice.click("#btn-announce-tcp")
        if not await carol.is_visible("#contacts"):
            await carol.click("#btn-menu")
        await carol.wait_for_selector("#contacts .contact[data-name='Alice']", timeout=15000)
        print("carol discovered alice over UDP broadcast")
        await carol.screenshot(path=f"{OUT}/11-carol-discovered.png")

        # Carol trusts Alice and writes; Alice hasn't trusted Carol -> contact request
        await carol.click("#contacts .contact[data-name='Alice'] .contact-trust")
        await carol.click(".peer-tab[data-tab=trusted]")
        await carol.click("#contacts .contact[data-name='Alice']")
        await carol.wait_for_function("!document.querySelector('#msg-input').disabled")
        await carol.fill("#msg-input", "Hi Alice, it's Carol from the LAN")
        await carol.click("#send-btn")
        await carol.wait_for_selector(".bubble-status.not_trusted", timeout=15000)
        print("carol sees 'waiting for them to accept you'")
        await carol.screenshot(path=f"{OUT}/12-carol-waiting-accept.png")

        await alice.click(".peer-tab[data-tab=discovered]")
        await alice.wait_for_selector("#contacts .contact[data-name='Carol'] .contact-request-tag", timeout=15000)
        await alice.screenshot(path=f"{OUT}/13-alice-contact-request.png")
        await alice.click("#contacts .contact[data-name='Carol'] .contact-trust")
        await alice.click(".peer-tab[data-tab=trusted]")
        await alice.click("#contacts .contact[data-name='Carol']")
        await alice.wait_for_selector("text=Hi Alice, it's Carol from the LAN", timeout=15000)
        print("alice accepted request and received carol's queued message")
        await carol.wait_for_selector(".bubble-status.delivered", timeout=15000)
        print("carol sees delivered")

        # Offline delivery: stop Carol, Alice sends, restart Carol
        NODES["carol"].stop()
        await alice.fill("#msg-input", "Sent while you were offline")
        await alice.click("#send-btn")
        await alice.wait_for_selector(".bubble.out:last-child .bubble-status:is(.pending,.sent)", timeout=5000)
        print("alice message queued while carol offline")
        NODES["carol"].start()
        await NODES["carol"].wait_ready()
        carol = await open_ui(browser, "carol", mobile=True)
        if not await carol.is_visible("#contacts"):
            await carol.click("#btn-menu")
        await carol.click(".peer-tab[data-tab=trusted]")
        await carol.click("#contacts .contact[data-name='Alice']")
        await carol.wait_for_selector("text=Sent while you were offline", timeout=45000)
        await carol.wait_for_selector("text=Hi Alice, it's Carol from the LAN", timeout=5000)
        print("carol got the offline message after restart; history persisted")
        await carol.screenshot(path=f"{OUT}/14-carol-mobile-chat.png")
        await alice.wait_for_selector(".bubble.out:last-child .bubble-status.delivered", timeout=15000)
        print("alice sees delivered")
        await browser.close()


async def main() -> int:
    global OUT
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="", help="directory for screenshots")
    args = parser.parse_args()
    base = Path(tempfile.mkdtemp(prefix="srltcp-sim-"))
    OUT = Path(args.out) if args.out else base / "screenshots"
    OUT.mkdir(parents=True, exist_ok=True)
    # Carol listens on the default discovery port so Alice's announce reaches her.
    NODES["alice"] = Node("alice", 19876, 17825, 17826, base)
    NODES["bob"] = Node("bob", 29876, 27825, 27826, base)
    NODES["carol"] = Node("carol", 39876, 37825, 7826, base)
    for n in NODES.values():
        n.start()
    try:
        for n in NODES.values():
            await n.wait_ready()
        await desktop_flow()
        await mobile_flow()
    finally:
        for n in NODES.values():
            n.stop()
    print(f"screenshots: {OUT}")
    if errors:
        print("browser errors:", *errors, sep="\n  ")
    print("SIMULATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
