"""One phone's browser for a video call inside its network namespace.

Used by scripts/simulate_internet.sh. Usage: sim_call_side.py caller|callee DATA_DIR FLAGDIR
"""

import asyncio
import os
import sys
import time

from playwright.async_api import async_playwright
role, data_dir, flags = sys.argv[1], sys.argv[2], sys.argv[3]
tok = open(f"{data_dir}/web_token").read().strip()
def flag(n): open(f"{flags}/{n}", "w").close()
async def wait_flag(n, t=60):
    end = time.time() + t
    while not os.path.exists(f"{flags}/{n}"):
        if time.time() > end: raise TimeoutError(n)
        await asyncio.sleep(0.2)
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/opt/pw-browsers/chromium",
            args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        ctx = await b.new_context(ignore_https_errors=True, bypass_csp=True, permissions=["camera", "microphone"],
                                  viewport={"width": 900, "height": 700})
        pg = await ctx.new_page()
        await pg.add_init_script("""(() => { const O = window.RTCPeerConnection; window.__pcs = [];
            window.RTCPeerConnection = function(...a) { const pc = new O(...a); window.__pcs.unshift(pc); return pc; };
            window.RTCPeerConnection.prototype = O.prototype; })();""")
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto(f"https://127.0.0.1:9876/?token={tok}"); await pg.wait_for_load_state("networkidle")
        if await pg.is_visible("#setup-overlay"):
            await pg.fill("#setup-name", role)
            await pg.click("#setup-form button[type=submit]")
            await pg.wait_for_selector("#setup-overlay", state="hidden")
        await pg.click(".peer-tab[data-tab=trusted]")
        await pg.wait_for_selector("#contacts .contact", timeout=15000)
        await pg.click("#contacts .contact")
        if role == "callee":
            flag("callee-ready")
            await pg.wait_for_selector("#call-overlay:not(.hidden)[data-mode=incoming]", timeout=30000)
            await pg.click("#call-accept-video")
        else:
            await wait_flag("callee-ready")
            await asyncio.sleep(1)
            await pg.click("#btn-call-video")
        await pg.wait_for_function("document.querySelector('#call-status').textContent.includes('encrypted')", timeout=30000)
        await asyncio.sleep(3)
        w = await pg.evaluate("document.querySelector('#call-remote-video').videoWidth")
        stats = await pg.evaluate("""async () => {
            const v = document.querySelector('#call-remote-video');
            return {width: v.videoWidth, playing: !v.paused, t: v.currentTime};
        }""")
        pair = await pg.evaluate("""async () => {
            const pc = window.__pcs && window.__pcs[0];
            if (!pc) return null;
            const st = await pc.getStats();
            let sel = null, cands = {};
            st.forEach(r => { if (r.type === 'local-candidate' || r.type === 'remote-candidate') cands[r.id] = r.candidateType; });
            st.forEach(r => { if (r.type === 'candidate-pair' && r.nominated && r.state === 'succeeded') sel = [cands[r.localCandidateId], cands[r.remoteCandidateId], r.bytesReceived]; });
            return sel;
        }""")
        print(role, "call connected; remote video", stats, "selected pair (local, remote, bytesRx):", pair)
        await pg.screenshot(path=f"{flags}/{role}-call.png")
        flag(f"{role}-done")
        await wait_flag("caller-done" if role == "callee" else "callee-done")
        await asyncio.sleep(1)
        if role == "caller":
            await pg.click("#call-hangup")
        await pg.wait_for_selector("#call-overlay", state="hidden", timeout=10000)
        print(role, "hung up cleanly; page errors:", errs)
        await b.close()
asyncio.run(main())
