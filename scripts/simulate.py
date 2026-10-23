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
        await asyncio.sleep(1)
        if await alice.is_visible("#pair-close"):
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
        await bob.click("text=Hey Bob, got it loud and clear", button="right")
        labels = await bob.locator("#msg-sheet-menu .msg-sheet-item span").all_inner_texts()
        assert "Copy" in labels and "Select text" not in labels, labels
        await bob.click("#msg-sheet", position={"x": 5, "y": 5})
        await bob.wait_for_selector("#msg-sheet", state="hidden", timeout=3000)
        print("message menu: Copy (no Select text)")
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
        kind = await alice.inner_text("#call-kind")
        assert await alice.is_visible("#call-accept-video") and "Video call" in kind, (
            await alice.is_visible("#call-accept-video"), kind)
        await shot(alice, "04-alice-incoming-call")
        # Alice's device has a front and a rear camera, but (like a real browser)
        # lists only one until a camera has been opened.
        await alice.evaluate("""(() => {
            const md = navigator.mediaDevices, real = md.enumerateDevices.bind(md);
            const gum = md.getUserMedia.bind(md);
            window.__realMedia = { enumerateDevices: md.enumerateDevices, getUserMedia: md.getUserMedia };
            let opened = false;
            md.getUserMedia = async (c) => { const s = await gum(c); if (c && c.video) opened = true; return s; };
            md.enumerateDevices = async () => { const d = await real();
              const cam = d.find((x) => x.kind === "videoinput");
              return cam && opened ? [...d, { kind: "videoinput", deviceId: "rear", label: "rear", groupId: "" }] : d; };
        })()""")
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
        await alice.wait_for_selector("#call-switch-camera:not(.hidden)", timeout=5000)
        print("answering phone shows the front/rear camera button")
        await alice.evaluate("Object.assign(navigator.mediaDevices, window.__realMedia)")  # one camera again
        await shot(alice, "05-alice-in-call")
        # Back to the chat while the call goes on: a small window with their video.
        await alice.click("#call-minimize")
        await alice.wait_for_selector("#call-overlay.mini.remote-playing", timeout=5000)
        assert await alice.is_visible("#msg-input")
        await alice.fill("#msg-input", "Typing during the video call")
        await alice.press("#msg-input", "Enter")
        await bob.wait_for_selector("text=Typing during the video call", timeout=10000)
        t0 = await alice.evaluate("document.querySelector('#call-remote-video').currentTime")
        await asyncio.sleep(1.5)
        t1 = await alice.evaluate("document.querySelector('#call-remote-video').currentTime")
        assert t1 > t0, ("video stopped in the small window", t0, t1)
        await shot(alice, "05b-alice-chat-during-call")
        box = await alice.locator("#call-overlay").bounding_box()
        await alice.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await alice.mouse.down()
        await alice.mouse.move(box["x"] - 120, box["y"] - 160, steps=6)
        await alice.mouse.up()
        moved = await alice.locator("#call-overlay").bounding_box()
        assert await alice.evaluate("document.querySelector('#call-overlay').classList.contains('mini')")
        assert abs(moved["x"] - box["x"]) > 50, (box, moved)  # dragged, still small
        await alice.click("#call-overlay", position={"x": 20, "y": 60})
        await alice.wait_for_function(
            "!document.querySelector('#call-overlay').classList.contains('mini')", timeout=5000)
        print("call: back to chat in a small window (video kept playing, message sent, "
              "window dragged), tap brings the call back")
        # Android's floating window (picture-in-picture): only their picture.
        await alice.evaluate("window.srltcpOnPip(true)")
        assert await alice.evaluate("document.querySelector('#call-overlay').classList.contains('pip')")
        assert not await alice.is_visible("#call-hangup") and not await alice.is_visible("#call-minimize")
        assert await alice.is_visible("#call-remote-video")
        await shot(alice, "05c-alice-pip")
        await alice.evaluate("window.srltcpOnPip(false)")
        assert await alice.is_visible("#call-hangup")
        print("call: floating-window layout shows only the picture, and comes back")
        # Network switch mid-call: Bob's node goes silent for longer than
        # WebRTC waits (~30 s). The call must hold on and resume by itself.
        import signal as _signal
        NODES["bob"].proc.send_signal(_signal.SIGSTOP)
        try:
            await alice.wait_for_function(
                "document.querySelector('#call-status').textContent.includes('Reconnecting')",
                timeout=40000)
            await asyncio.sleep(35)
            assert await alice.is_visible("#call-overlay"), "call ended during the outage"
        finally:
            NODES["bob"].proc.send_signal(_signal.SIGCONT)
        for pg in (alice, bob):
            await pg.wait_for_function(
                "document.querySelector('#call-status').textContent.includes('encrypted')",
                timeout=60000)
        # (After a 35 s outage the picture takes a few seconds to come back
        # (measured: ~3 s after the call says it's reconnected); within 10 s.)
        t0 = await alice.evaluate("document.querySelector('#call-remote-video').currentTime")
        await alice.wait_for_function(
            f"document.querySelector('#call-remote-video').currentTime > {t0} + 0.5", timeout=10000)
        print("call survived a 35 s outage (network switch) and resumed with video")
        await bob.click("#call-hangup")
        await alice.wait_for_selector("#call-overlay", state="hidden", timeout=10000)
        print("call ended on both sides")

        # --- Answer from the Android notification (before the page has the
        # offer, as when the app opens from the notification) + rear camera ---
        await alice.evaluate("window.onAndroidAnswerCall()")
        await bob.evaluate("""(() => {  // pretend Bob's device has a front and a rear camera
            const md = navigator.mediaDevices, real = md.enumerateDevices.bind(md);
            md.enumerateDevices = async () => { const d = await real();
              const cam = d.find((x) => x.kind === "videoinput");
              return cam ? [...d, { kind: "videoinput", deviceId: "rear", label: "rear", groupId: "" }] : d; };
        })()""")
        await bob.click("#btn-call-video")
        for pg in (alice, bob):
            await pg.wait_for_function(
                "document.querySelector('#call-status').textContent.includes('encrypted')", timeout=20000)
        print("notification Answer: call answered with no tap in the page")
        await bob.wait_for_selector("#call-switch-camera:not(.hidden)", timeout=5000)
        assert await alice.is_hidden("#call-switch-camera")  # one camera: no switch button
        sender = "(() => { const v = document.querySelector('#call-local-video').srcObject;"
        sender += " return v && v.getVideoTracks()[0] && v.getVideoTracks()[0].id; })()"
        before = await bob.evaluate(sender)
        await bob.click("#call-switch-camera")
        await bob.wait_for_selector("#call-overlay.rear-camera", timeout=5000)
        after = await bob.evaluate(sender)
        assert before and after and before != after, (before, after)
        assert await bob.get_attribute("#call-switch-camera", "aria-label") == "Switch to the front camera"
        t0 = await alice.evaluate("document.querySelector('#call-remote-video').currentTime")
        await asyncio.sleep(2)
        assert await alice.evaluate("document.querySelector('#call-remote-video').currentTime") > t0
        await bob.click("#call-switch-camera")
        await bob.wait_for_function("!document.querySelector('#call-overlay').classList.contains('rear-camera')")
        print("camera switch: rear and back to front, video keeps flowing")
        await bob.click("#call-hangup")
        await alice.wait_for_selector("#call-overlay", state="hidden", timeout=10000)

        # --- Voice call declined ---
        await alice.click("#btn-call-voice")
        await bob.wait_for_selector("#call-overlay:not(.hidden)[data-mode=incoming]", timeout=10000)
        assert await bob.is_visible("text=Voice call") and not await bob.is_visible("#call-accept-video")
        print("voice call: callee sees a voice call with a phone answer button only")
        await bob.click("#call-decline")
        await alice.wait_for_selector("#call-overlay", state="hidden", timeout=10000)
        print("declined call handled")

        await browser.close()


async def decline_requests(page) -> None:  # type: ignore[no-untyped-def]
    """Close contact-request prompts from contacts deleted earlier in the run."""
    for _ in range(5):
        if not await page.is_visible("#request-modal.open"):
            return
        await page.click("#request-decline")
        await page.wait_for_timeout(300)


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

        # Verify each other (paired with a pasted link, so not verified yet):
        # Bob compares the numbers, Alice "scans" Bob's code.
        for pg in (alice, bob):
            await pg.wait_for_selector("#chat-peer-verified:not(.hidden):not(.verified)")
            await pg.click("#chat-peer-verified")
            await pg.wait_for_selector("#verify-number span:nth-child(12)", timeout=5000)
        numbers = [await pg.text_content("#verify-number") for pg in (alice, bob)]
        assert numbers[0] == numbers[1] and len(numbers[0] or "") == 60, numbers
        await shot(bob, "06-verify-contact")
        await bob.click("#verify-confirm")
        await bob.wait_for_selector("#verify-remove:not(.hidden)", timeout=5000)
        alice_hash = await alice.evaluate(
            "async () => (await (await fetch('/api/identities')).json()).tcp.hash_id"
        )
        bob_code = await bob.evaluate(  # what Alice's camera would read off Bob's screen
            "async (h) => (await (await fetch(`/api/trusted/${h}/safety`)).json()).code",
            alice_hash,
        )
        await alice.evaluate("code => window.onAndroidQrResult(code)", bob_code)
        await alice.wait_for_selector("#verify-status.ok", timeout=5000)
        for pg in (alice, bob):
            await pg.click("#verify-close")
            await pg.wait_for_selector("#chat-peer-verified.verified", timeout=5000)
        print("verify: safety numbers match on both phones; confirmed and scanned")

        # Several files picked at once
        picked = [OUT / "one.txt", OUT / "two.txt"]
        for f in picked:
            f.write_text(f"{f.name}\n" * 1000)
        await alice.set_input_files("#file-input", [str(f) for f in picked])
        for f in picked:
            await bob.wait_for_selector(f".file-bubble:has-text('{f.name}')", timeout=20000)
        print("multi-file: two files picked together, both arrived")

        # Folder sync: Alice starts it from the contact menu, Bob accepts.
        sync_src = NODES["alice"].data / "syncme"
        (sync_src / "sub").mkdir(parents=True, exist_ok=True)
        (sync_src / "a.txt").write_text("synced file\n")
        (sync_src / "sub" / "b.txt").write_text("in a subfolder\n")
        await alice.click("#contacts .contact[data-name='Bobby'] .contact-more")
        await alice.click("#contact-menu [data-action=sync]")
        await alice.wait_for_selector("#sync-modal.open")
        await alice.evaluate(
            "p => { document.querySelector('#sync-new-path').value = p; }", str(sync_src)
        )
        await alice.fill("#sync-new-name", "Holiday")
        await alice.click("#sync-new-start")
        await alice.wait_for_selector("#sync-list >> text=Waiting for", timeout=5000)
        await bob.wait_for_selector("#sync-modal.open [data-sync-accept]", timeout=10000)
        await shot(bob, "08-sync-invite")
        await bob.click("#sync-modal [data-sync-accept]")
        synced_b = NODES["bob"].data / "transfers" / "Sync" / "Holiday"
        deadline = time.time() + 30
        while time.time() < deadline and not (synced_b / "sub" / "b.txt").exists():
            await asyncio.sleep(0.5)
        assert (synced_b / "a.txt").read_text() == "synced file\n"
        assert (synced_b / "sub" / "b.txt").read_text() == "in a subfolder\n"
        await alice.wait_for_selector("#sync-list >> text=up to date", timeout=15000)
        for pg in (alice, bob):
            await pg.click("#sync-close")
        print("folder sync: invited, accepted, both files arrived")

        # Shared folder: Alice offers, Bob browses and downloads
        await alice.click("#btn-share-folder")
        await alice.wait_for_selector("#share-submit")
        if not await alice.is_checked("#share-recipients input[type=checkbox]"):
            await alice.check("#share-recipients input[type=checkbox]")
        await alice.click("#share-submit")
        await alice.wait_for_selector("#share-mine-list .share-grant-item", timeout=10000)
        await alice.click("#share-close")
        await bob.wait_for_selector("text=shared “", timeout=10000)
        await bob.click("#btn-share-folder")
        await bob.click(".share-tab[data-share-tab=incoming]")
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
        # Progress, speed and the cancel button live in the file's own bubble.
        cancel_big = ".file-bubble:has-text('big.bin') [data-cancel-transfer]:not([disabled])"
        await alice.wait_for_selector(cancel_big, timeout=30000)
        await bob.wait_for_selector(".file-bubble:has-text('big.bin') .progress-fill", timeout=30000)
        assert await alice.locator("[data-cancel-transfer]").count() == 1, "one transfer view only"
        await shot(alice, "05-transfer-in-progress")
        await alice.click(cancel_big)
        cancelled = (
            "[...document.querySelectorAll('.file-bubble')]"
            ".some(b => b.textContent.includes('big.bin') && /cancel/i.test(b.textContent))"
        )
        await alice.wait_for_function(cancelled, timeout=10000)
        await bob.wait_for_function(cancelled, timeout=10000)  # the receiver sees it too
        print("transfer: cancelled mid-flight, on both sides")
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
        await alice.click("#delete-chat-submit")  # (it asks first, like Signal)
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

        # Privacy: disappearing messages on, a backup made through the API
        await alice.click("#btn-settings")
        await alice.click(".settings-tab[data-tab=privacy]")
        await alice.select_option("#set-disappear", "3600")
        await shot(alice, "07-privacy-settings")
        await alice.click("#settings-form button[type=submit]")
        await alice.wait_for_selector("text=Settings saved", timeout=5000)
        await alice.keyboard.press("Escape")
        await alice.click("#contacts .contact[data-name='Bobby']")
        await alice.fill("#msg-input", "this one disappears")
        await alice.press("#msg-input", "Enter")
        await alice.wait_for_selector(".bubble-row:has-text('this one disappears') .bubble-timer")
        await bob.wait_for_selector(
            ".bubble-row:has-text('this one disappears') .bubble-timer", timeout=10000
        )
        made = await alice.evaluate(
            "async () => (await fetch('/api/backup', {method: 'POST', headers: "
            "{'Content-Type': 'application/json'}, body: JSON.stringify({password: "
            "'sim backup pass'})})).json()"
        )
        assert made.get("filename", "").endswith(".srltcp"), made
        await alice.click("#btn-settings")
        await alice.click(".settings-tab[data-tab=privacy]")
        await alice.select_option("#set-disappear", "0")
        await alice.click(".settings-tab[data-tab=general]")  # where the next step starts
        await alice.click("#settings-form button[type=submit]")
        await alice.wait_for_selector("text=Settings saved", timeout=5000)
        await alice.keyboard.press("Escape")
        print("privacy: disappearing timer shown on both sides; encrypted backup created")
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
        # Bob (deleted from Alice's contacts earlier) may ask first: answer later.
        for _ in range(30):
            if await alice.is_visible("#request-modal.open >> text=Carol"):
                break
            if await alice.is_visible("#request-modal.open"):
                await alice.click("#request-decline")
            await alice.wait_for_timeout(500)
        await alice.wait_for_selector("#request-modal.open >> text=Carol", timeout=15000)
        await alice.screenshot(path=f"{OUT}/13-alice-contact-request.png")
        await alice.click("#request-accept")
        await alice.wait_for_selector("#request-modal.open >> text=Carol", state="detached", timeout=5000)
        if await alice.is_visible("#request-modal.open"):
            await alice.click("#request-decline")  # Bob's request again
        if not await alice.is_visible(".peer-tab[data-tab=trusted]") and await alice.is_visible("#btn-back"):
            await alice.click("#btn-back")  # phone layout: back from the open chat
        await alice.click(".peer-tab[data-tab=trusted]")
        await alice.click("#contacts .contact[data-name='Carol']")
        await alice.wait_for_selector("text=Hi Alice, it's Carol from the LAN", timeout=15000)
        print("alice accepted request and received carol's queued message")
        await carol.wait_for_selector(".bubble-status.delivered", timeout=15000)
        print("carol sees delivered")

        # Offline delivery: stop Carol, Alice sends, restart Carol
        NODES["carol"].stop()
        await decline_requests(alice)  # Bob (deleted earlier) keeps asking
        await alice.fill("#msg-input", "Sent while you were offline")
        await alice.click("#send-btn")
        await alice.wait_for_selector(".bubble.out:last-child .bubble-status:is(.pending,.sent)", timeout=5000)
        print("alice message queued while carol offline")
        # A file to an unreachable contact waits too (it used to fail outright).
        note = OUT / "offline-note.txt"
        note.write_text("sent while carol was offline\n")
        await alice.set_input_files("#file-input", str(note))
        try:
            await alice.wait_for_selector(
                ".file-bubble:has-text('offline-note.txt') >> text=waiting for connection", timeout=15000)
        except Exception:
            print("DEBUG bubbles:", await alice.evaluate(
                "[...document.querySelectorAll('.file-bubble')].map(b => b.innerText).join(' | ')"))
            await alice.screenshot(path=f"{OUT}/debug-alice.png")
            raise
        print("alice file queued while carol offline")
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
        await carol.wait_for_selector(".file-bubble:has-text('offline-note.txt') >> text=complete", timeout=45000)
        print("carol got the file queued while she was offline")
        await carol.screenshot(path=f"{OUT}/14-carol-mobile-chat.png")
        await alice.wait_for_selector(
            ".bubble.out:has-text('Sent while you were offline') .bubble-status.delivered", timeout=15000)
        print("alice sees delivered")
        await browser.close()


async def group_flow() -> None:
    """Alice makes a group with Bob and Carol, who are not each other's contacts."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(**LAUNCH)
        alice = await open_ui(browser, "alice")
        bob = await open_ui(browser, "bob")
        carol = await open_ui(browser, "carol", mobile=True)
        # Alice and Bob pair again (Bob was deleted in the features flow).
        # Bob may already be asking to be added back: decline that (it only
        # hides the request); pairing explicitly still works.
        await decline_requests(alice)
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

        # Network settings: direct only (no relay or call-server fields), saves fine.
        page = await open_ui(browser, "alice")
        await page.click("#btn-settings")
        await page.click(".settings-tab[data-tab=network]")
        for gone in ("#set-relay-code", "#set-relay-enabled", "#set-stun", "#set-turn-user"):
            assert await page.locator(gone).count() == 0, gone
        await page.click("#settings-form button[type=submit]")
        await page.wait_for_selector("text=Settings saved", timeout=5000)
        await page.locator(".net-settings").first.screenshot(path=f"{OUT}/30-network-settings.png")
        print("network settings: no relay options, saved")
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
        # Keep the screenshots only: node data and the 600 MB test file go.
        import shutil

        for n in NODES.values():
            shutil.rmtree(n.data, ignore_errors=True)
        (OUT / "big.bin").unlink(missing_ok=True)
    print(f"screenshots: {OUT}")
    if errors:
        print("browser errors:", *errors, sep="\n  ")
    print("SIMULATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
