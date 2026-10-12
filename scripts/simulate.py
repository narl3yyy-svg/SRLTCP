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


async def open_ui(browser, name: str, mobile: bool = False, csp_bypass: bool = True):  # type: ignore[no-untyped-def]
    vp = {"width": 390, "height": 844} if mobile else {"width": 1280, "height": 800}
    ctx = await browser.new_context(
        ignore_https_errors=True, bypass_csp=csp_bypass, viewport=vp, is_mobile=mobile,
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
        # "Download file" asks where to save (stub the picker, collect the bytes)
        await alice.evaluate("""() => {
          window.__saved = null;
          window.showSaveFilePicker = async (opts) => ({
            createWritable: async () => {
              const parts = [];
              return new WritableStream({
                write: (c) => { parts.push(c); },
                close: () => { window.__saved = { name: opts.suggestedName,
                  size: parts.reduce((n, c) => n + c.byteLength, 0) }; },
              });
            },
          });
        }""")
        await alice.click(".bubble.in .file-download")
        await alice.wait_for_function("window.__saved && window.__saved.size > 0", timeout=10000)
        saved = await alice.evaluate("window.__saved")
        assert saved["size"] == os.path.getsize(f), saved
        print("download: save picker offered, saved", saved)

        # --- Video call ---
        await bob.click("#btn-call-video")
        await alice.wait_for_selector("#call-overlay:not(.hidden)[data-mode=incoming]", timeout=10000)
        assert await alice.is_visible("#call-accept-video") and await alice.is_visible("text=Video call")
        await shot(alice, "04-alice-incoming-call")
        await alice.click("#call-accept-video")
        await alice.wait_for_function("document.querySelector('#call-status').textContent.includes('encrypted')", timeout=20000)
        await bob.wait_for_function("document.querySelector('#call-status').textContent.includes('encrypted')", timeout=20000)
        await asyncio.sleep(2)
        vw = await alice.evaluate("document.querySelector('#call-remote-video').videoWidth")
        print("call connected; alice receives remote video width", vw)
        await alice.wait_for_selector("#call-overlay.remote-playing", timeout=10000)
        playing = await alice.evaluate(
            "(() => { const a = document.querySelector('#call-remote-audio');"
            " return !a.paused && a.srcObject && a.srcObject.getAudioTracks().length > 0; })()"
        )
        assert playing, "remote audio is not playing"
        assert await alice.evaluate("document.querySelector('#call-remote-video').muted")
        # Mic starts on; mute turns the button red, unmute restores it
        assert await alice.get_attribute("#call-mute", "aria-pressed") == "false"
        await alice.click("#call-mute")
        assert await alice.get_attribute("#call-mute", "aria-pressed") == "true"
        bg = await alice.evaluate("getComputedStyle(document.querySelector('#call-mute')).backgroundColor")
        assert bg == "rgb(240, 84, 106)", bg
        await alice.click("#call-mute")
        print("call: remote audio playing, picture shown, mic on by default, mute is red")
        await shot(alice, "05-alice-in-call")
        await bob.click("#call-hangup")
        await alice.wait_for_selector("#call-overlay", state="hidden", timeout=10000)
        print("call ended on both sides")

        # --- Voice call declined ---
        await alice.click("#btn-call-voice")
        await bob.wait_for_selector("#call-overlay:not(.hidden)[data-mode=incoming]", timeout=10000)
        assert await bob.is_visible("text=Voice call") and not await bob.is_visible("#call-accept-video")
        print("voice call: callee sees a voice call with a phone answer button only")
        await bob.click("#call-decline")
        await alice.wait_for_selector("#call-overlay", state="hidden", timeout=10000)
        print("declined call handled")

        await browser.close()


async def features_flow() -> None:
    """Contacts, shared folders, folder send, cancel, block, settings, misc modals."""
    shared = NODES["alice"].data / "shared"
    (shared / "docs").mkdir(parents=True, exist_ok=True)
    (shared / "hello.txt").write_text("hello from alice's shared folder\n")
    (shared / "docs" / "notes.md").write_text("# notes\n")
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH)
        alice = await open_ui(browser, "alice")
        bob = await open_ui(browser, "bob")
        for pg in (alice, bob):
            pg.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await pg.click(".peer-tab[data-tab=trusted]")
        await alice.click("#contacts .contact[data-name='Bob']")
        await bob.click("#contacts .contact[data-name='Alice']")

        # Edit contact from the chat header
        await alice.click("#chat-peer-name")
        await alice.wait_for_selector("#edit-contact-modal.open")
        await alice.fill("#edit-contact-name", "Bobby")
        await alice.click("#edit-contact-save")
        await alice.wait_for_selector("#contacts .contact[data-name='Bobby']", timeout=5000)
        assert await alice.text_content("#chat-peer-name") == "Bobby"
        print("edit contact: renamed Bob -> Bobby")
        # A LAN/VPN IP in the internet field gets a helpful error
        await alice.click("#contacts .contact[data-name='Bobby'] .contact-more")
        await alice.click("#contact-menu [data-action=edit]")
        await alice.evaluate("document.querySelector('#edit-contact-modal details').open = true")
        await alice.fill("#edit-contact-wan-host", "10.0.100.5")
        await alice.click("#edit-contact-save")
        await alice.wait_for_selector("text=put it in the Address field", timeout=5000)
        await alice.fill("#edit-contact-wan-host", "")
        await alice.click("#edit-contact-cancel")
        print("edit contact: VPN IP in internet field explained")

        # Shared folder: Alice offers, Bob browses and downloads
        await alice.click("#btn-share-folder")
        await alice.wait_for_selector("#share-offer-btn")
        await alice.click("#share-offer-btn")
        await alice.click("#share-close")
        await bob.wait_for_selector("text=Shared folder offered", timeout=10000)
        await bob.click("#btn-share-folder")
        await bob.wait_for_selector(".share-grant-btn", timeout=5000)
        await bob.click(".share-grant-btn")
        await bob.wait_for_selector(".share-entry.file >> text=hello.txt", timeout=10000)
        await bob.click(".share-entry.file >> text=hello.txt")
        await bob.click("#share-close")
        await bob.wait_for_selector(".file-bubble >> text=hello.txt", timeout=15000)
        print("shared folder: offered, browsed, downloaded hello.txt")

        # Cancel a transfer in flight (big file, throttled link not needed: cancel immediately)
        big = OUT / "big.bin"
        with open(big, "wb") as fh:  # large enough to still be in flight when we cancel
            for _ in range(600):
                fh.write(os.urandom(1024 * 1024))
        await alice.set_input_files("#file-input", str(big))
        await alice.wait_for_selector("[data-cancel-transfer]", timeout=30000)
        await alice.click("[data-cancel-transfer]")
        await alice.wait_for_function(
            "[...document.querySelectorAll('.file-bubble')].some(b => /cancel/i.test(b.textContent))",
            timeout=10000,
        )
        print("transfer: cancelled mid-flight")
        big.unlink(missing_ok=True)

        # Block / unblock
        await bob.click("#contacts .contact[data-name='Alice'] .contact-more")
        await bob.click("#contact-menu [data-action=block]")
        await bob.wait_for_selector("#contacts .contact[data-name='Alice'] >> text=blocked", timeout=5000)
        await bob.click("#contacts .contact[data-name='Alice'] .contact-more")
        await bob.click("#contact-menu [data-action=block]")
        await bob.wait_for_function(
            "!document.querySelector(\"#contacts .contact[data-name='Alice']\").textContent.includes('blocked')",
            timeout=5000,
        )
        print("block: blocked and unblocked")

        # Clear chat
        await alice.click("#contacts .contact[data-name='Bobby'] .contact-more")
        await alice.click("#contact-menu [data-action=clear-chat]")
        await alice.wait_for_selector("text=No messages yet", timeout=5000)
        print("clear chat: history cleared")

        # Settings persist across reload
        await alice.click("#btn-settings")
        await alice.fill("#set-name", "Alice Renamed")
        await alice.click("#settings-form button[type=submit]")
        await alice.wait_for_selector("text=Settings saved", timeout=5000)
        await alice.reload()
        await alice.wait_for_load_state("networkidle")
        assert (await alice.text_content("#me-name")).strip() == "Alice Renamed"
        print("settings: name saved and persisted")
        await alice.click("#btn-settings")
        await alice.fill("#set-name", "Alice")
        await alice.click("#settings-form button[type=submit]")
        await alice.wait_for_selector("text=Settings saved", timeout=5000)
        await alice.click("#btn-close-settings")

        # Misc modals and search
        await alice.click("#stat-version")
        await alice.wait_for_selector("#release-modal.open")
        await alice.click("#release-close")
        await alice.click("#btn-settings")
        await alice.click(".settings-tab[data-tab=network]")
        await alice.click("#btn-network-viz")
        await alice.wait_for_selector("#network-modal.open")
        await alice.click("#network-close")
        await alice.click("#btn-close-settings")
        await alice.click(".peer-tab[data-tab=trusted]")
        await alice.fill("#peer-search", "zzz-nobody")
        await alice.wait_for_selector("#contacts .contacts-empty")
        await alice.fill("#peer-search", "bob")
        await alice.wait_for_selector("#contacts .contact[data-name='Bobby']")
        print("misc: release notes, network map, search")

        # Delete contact from the edit screen
        await alice.click("#contacts .contact[data-name='Bobby'] .contact-more")
        await alice.click("#contact-menu [data-action=edit]")
        await alice.click("#edit-contact-delete")
        await alice.wait_for_selector("#contacts .contact[data-name='Bobby']", state="detached", timeout=5000)
        print("delete contact: removed")
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

        # Back gesture on the Pair screen closes it instead of leaving the app
        await carol.click("#btn-pair")
        await carol.wait_for_selector("#pair-modal.open")
        assert await carol.evaluate("window.androidNavigateBack()") is True
        await carol.wait_for_selector("#pair-modal.open", state="detached", timeout=5000)
        assert await carol.evaluate("window.androidNavigateBack()") is False  # main screen
        print("back gesture: pair screen closed, main screen stays")

        # Carol trusts Alice and writes; Alice hasn't trusted Carol -> contact request
        await carol.click("#contacts .contact[data-name='Alice'] .contact-trust")
        await carol.click(".peer-tab[data-tab=trusted]")
        await carol.click("#contacts .contact[data-name='Alice']")
        await carol.wait_for_function("!document.querySelector('#msg-input').disabled")
        await carol.fill("#msg-input", "Hi Alice, it's Carol from the LAN")
        await carol.tap("#send-btn")  # a real finger tap on the arrow
        await carol.wait_for_selector(".bubble-status.not_trusted", timeout=15000)
        print("carol sees 'waiting for them to accept you'")
        await carol.screenshot(path=f"{OUT}/12-carol-waiting-accept.png")

        # Alice is asked right away whether to add Carol back
        await alice.wait_for_selector("#request-modal.open >> text=Carol", timeout=15000)
        await alice.screenshot(path=f"{OUT}/13-alice-contact-request.png")
        await alice.click("#request-accept")
        await alice.wait_for_selector("#request-modal.open", state="detached", timeout=5000)
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
        # With a trusted contact the app opens on Trusted
        await carol.wait_for_selector(".peer-tab.active[data-tab=trusted]", timeout=10000)
        print("default tab: Trusted once a contact exists")
        await carol.click("#contacts .contact[data-name='Alice']")
        await carol.wait_for_selector("text=Sent while you were offline", timeout=45000)
        await carol.wait_for_selector("text=Hi Alice, it's Carol from the LAN", timeout=5000)
        print("carol got the offline message after restart; history persisted")
        await carol.screenshot(path=f"{OUT}/14-carol-mobile-chat.png")
        await alice.wait_for_selector(".bubble.out:last-child .bubble-status.delivered", timeout=15000)
        print("alice sees delivered")
        await browser.close()


async def group_flow() -> None:
    """Alice makes a group with Bob and Carol, who are not each other's contacts."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH)
        alice = await open_ui(browser, "alice")
        bob = await open_ui(browser, "bob")
        carol = await open_ui(browser, "carol", mobile=True)
        # Alice and Bob pair again (Bob was deleted in the features flow)
        await alice.click("#btn-pair")
        await alice.wait_for_selector("#pair-qr img", timeout=10000)
        await alice.click("#pair-copy")
        link = await alice.evaluate("navigator.clipboard.readText()")
        await alice.click("#pair-close")
        await bob.click("#btn-pair")
        await bob.fill("#pair-code-input", link)
        await bob.click("#pair-import")
        await bob.wait_for_selector("#chat-active:not(.hidden)", timeout=10000)
        await alice.click(".peer-tab[data-tab=trusted]")
        await alice.wait_for_selector("#contacts .contact[data-name='Bob']", timeout=15000)

        await alice.click(".peer-tab[data-tab=groups]")
        await alice.click("#group-new")
        await alice.fill("#group-name", "Weekend plans")
        await alice.check("#group-member-list label:has-text('Bob') input")
        await alice.check("#group-member-list label:has-text('Carol') input")
        await alice.click("#group-save")
        await alice.wait_for_selector("#chat-active.group-chat", timeout=10000)
        assert (await alice.text_content("#chat-peer-name")).strip() == "Weekend plans"
        assert not await alice.is_visible("#btn-call-voice")
        print("group: created by alice with bob and carol")

        await bob.wait_for_selector("text=You were added to the group", timeout=15000)
        await bob.click(".peer-tab[data-tab=groups]")
        await bob.click("#contacts [data-group]")
        await bob.fill("#msg-input", "Hi all, Bob here")
        await bob.click("#send-btn")

        if not await carol.is_visible("#contacts"):
            await carol.click("#btn-menu")
        await carol.click(".peer-tab[data-tab=groups]")
        await carol.wait_for_selector("#contacts [data-group] .unread-badge", timeout=15000)
        await carol.tap("#contacts [data-group]")
        await carol.wait_for_selector(".bubble.in >> text=Hi all, Bob here", timeout=15000)
        sender = await carol.text_content(".bubble.in .bubble-sender")
        assert sender.strip() == "Bob", sender
        print("group: carol got bob's message through alice, labelled 'Bob'")
        await carol.fill("#msg-input", "Carol says hi")
        await carol.tap("#send-btn")
        await bob.wait_for_selector(".bubble.in >> text=Carol says hi", timeout=15000)
        await alice.wait_for_selector(".bubble.in >> text=Carol says hi", timeout=15000)
        # Group chat stays separate from the one-to-one chat with Carol
        await alice.click(".peer-tab[data-tab=trusted]")
        await alice.click("#contacts .contact[data-name='Carol']")
        await alice.wait_for_selector("#chat-active:not(.group-chat)")
        assert not await alice.is_visible("text=Carol says hi")
        await carol.screenshot(path=f"{OUT}/20-carol-group-chat.png")
        print("group: replies reach everyone; one-to-one chats unaffected")

        # Bob leaves; Alice's member list updates
        await bob.click("#chat-peer-name")
        await bob.wait_for_selector("#group-modal.open")
        bob.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await bob.click("#group-leave")
        await bob.wait_for_selector("text=You are no longer in this group", timeout=10000)
        await alice.click(".peer-tab[data-tab=groups]")
        await alice.wait_for_function(
            "document.querySelector('#contacts [data-group] .contact-endpoint')?.textContent.startsWith('2 members')",
            timeout=15000,
        )
        print("group: bob left, alice sees 2 members")
        await browser.close()


async def csp_flow() -> None:
    """Click through the main screens with the real Content-Security-Policy on
    (the flows above bypass it because Playwright's helpers need eval)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH)
        for mobile in (False, True):
            before = len(errors)
            page = await open_ui(browser, "alice", mobile=mobile, csp_bypass=False)
            if mobile and not await page.is_visible("#contacts"):
                await page.click("#btn-menu")
            for tab in ("discovered", "trusted", "groups"):
                await page.click(f".peer-tab[data-tab={tab}]")
            await page.click("#group-new")
            await page.click("#group-cancel")
            await page.click(".peer-tab[data-tab=trusted]")
            await page.click("#contacts .contact >> nth=0")
            await page.click("#btn-pair") if not mobile else None
            if not mobile:
                await page.wait_for_selector("#pair-qr img", timeout=10000)
                await page.click("#pair-close")
            await page.wait_for_timeout(500)
            csp = [e for e in errors[before:] if "Content Security Policy" in e or "Refused" in e]
            assert not csp, csp
        print("csp: no Content-Security-Policy violations on desktop and phone layouts")

        # Relay settings: a damaged code is refused with a clear message.
        page = await open_ui(browser, "alice")
        await page.click("#btn-settings")
        await page.click(".settings-tab[data-tab=network]")
        await page.fill("#set-relay-code", "srltcp-relay://relay.example.org:7827#broken")
        await page.click("#settings-form button[type=submit]")
        await page.wait_for_selector("text=Internet relay:", timeout=5000)
        await page.fill("#set-relay-code", "")
        await page.click("#settings-form button[type=submit]")
        await page.wait_for_selector("text=Settings saved", timeout=5000)
        await page.locator(".relay-settings").screenshot(path=f"{OUT}/30-relay-settings.png")
        print("relay settings: bad code rejected, empty code saved")
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
        await features_flow()
        await mobile_flow()
        await group_flow()
        await csp_flow()
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
