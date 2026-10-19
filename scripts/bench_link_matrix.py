"""Run scripts/bench_link.py for every radio over several seeds (a few at a
time) and summarise: median speed, worst urgent-message delay, overflow.

Usage: .venv/bin/python scripts/bench_link_matrix.py [--seeds 3] [--kb 96] [radio ...]
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def one(radio: str, seed: int, kb: int, app: bool = False) -> dict:
    script = "scripts/bench_radio.py" if app else "scripts/bench_link.py"
    out = subprocess.run(
        [sys.executable, str(ROOT / script), radio, "--kb", str(kb), "--seed", str(seed)],
        capture_output=True, text=True, timeout=1200, cwd=ROOT,
    ).stdout
    lines = [line for line in out.splitlines() if line.startswith("{")]
    return json.loads(lines[-1]) if lines else {"radio": radio, "Bps": 0, "complete": False}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("radios", nargs="*",
                        default=["cable", "sik", "sik-ecc", "lora", "noisy"])
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--kb", type=int, default=96)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--app", action="store_true", help="two full app nodes (bench_radio)")
    args = parser.parse_args()
    jobs = [(r, s) for r in args.radios for s in range(1, args.seeds + 1)]
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda j: one(j[0], j[1], args.kb, args.app), jobs))
    if args.app:
        for radio in args.radios:
            rs = [r for r in results if r.get("radio") == radio]
            print(json.dumps({
                "radio": radio,
                "connect_s": [r.get("connect_s") for r in rs],
                "file_Bps": [r.get("file_Bps") for r in rs],
                "chat_idle_s": [r.get("chat_idle_s") for r in rs],
                "chat_busy_s": [r.get("chat_busy_s") for r in rs],
                "ping_ms": [r.get("ping_ms") for r in rs],
                "overflow": [r.get("air", {}).get("overflow") for r in rs],
            }), flush=True)
        return
    for radio in args.radios:
        rs = [r for r in results if r.get("radio") == radio]
        urgent = [u for r in rs for u in r.get("urgent_s", [])]
        print(json.dumps({
            "radio": radio,
            "Bps_median": statistics.median(r.get("Bps", 0) for r in rs),
            "Bps_all": [r.get("Bps", 0) for r in rs],
            "all_complete": all(r.get("complete") for r in rs),
            "urgent_median": statistics.median(urgent) if urgent else None,
            "urgent_max": max(urgent) if urgent else None,
            "overflow": [r.get("air", {}).get("overflow") for r in rs],
            "given_up": sum(r.get("given_up", 0) for r in rs),
        }), flush=True)


if __name__ == "__main__":
    main()
