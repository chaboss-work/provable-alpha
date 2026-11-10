"""Run the scout, buyer and verifier side by side (one thread each) and keep them alive.

    python run_all.py --threshold 6 --horizon 600 --min-gap 900
"""
from __future__ import annotations

import argparse
import threading
import time

from buyer import Buyer
from pa.util import log
from scout import Scout
from verifier import Verifier


def loop(name: str, step, interval: int) -> None:
    while True:
        try:
            step()
        except Exception as e:
            log(name, f"error: {e}")
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=6.0)
    ap.add_argument("--horizon", type=int, default=600)
    ap.add_argument("--min-gap", type=int, default=900, help="seconds between two commits")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()
    scout = Scout(threshold_bps=args.threshold, horizon_s=args.horizon, min_gap_s=args.min_gap)
    buyer = Buyer()
    buyer.ensure_key()
    verifier = Verifier()

    def scout_step():
        scout.measure()
        scout.service()

    def buyer_step():
        buyer.shop()
        buyer.follow_up()

    threads = [
        threading.Thread(target=loop, args=("scout", scout_step, args.interval), daemon=True),
        threading.Thread(target=loop, args=("buyer", buyer_step, 20), daemon=True),
        threading.Thread(target=loop, args=("verifier", verifier.step, 30), daemon=True),
    ]
    for t in threads:
        t.start()
        time.sleep(3)
    log("runner", f"agents running: threshold={args.threshold}bps horizon={args.horizon}s min_gap={args.min_gap}s")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
