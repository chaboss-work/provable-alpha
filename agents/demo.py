"""One full signal lifecycle on testnet in a few minutes, for the demo video.

    python demo.py --horizon 90            # wait for a real signal (threshold 6 bps)
    python demo.py --horizon 90 --force    # commit even if the deviation is small (payload says forced=true)
"""
from __future__ import annotations

import argparse
import time

from buyer import Buyer
from pa import near
from pa.util import log, now_ms
from scout import Scout
from verifier import Verifier


def wait(seconds: float, why: str) -> None:
    log("demo", f"waiting {int(seconds)}s: {why}")
    time.sleep(max(0, seconds))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=90)
    ap.add_argument("--threshold", type=float, default=6.0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-tries", type=int, default=20)
    args = ap.parse_args()

    scout = Scout(threshold_bps=args.threshold, horizon_s=args.horizon, reveal_grace_s=600)
    buyer = Buyer()
    verifier = Verifier()
    log("demo", f"contract {near.CONTRACT} on {near.NETWORK}")

    while not scout.baseline.ready:
        scout.measure()
        wait(10, "building the Intents-vs-CEX baseline")

    signal_id = None
    for i in range(args.max_tries):
        signal_id = scout.measure(forced=args.force and i == args.max_tries - 1 or args.force)
        if signal_id is not None:
            break
        wait(15, "no deviation above threshold yet")
    if signal_id is None:
        log("demo", "no signal; rerun with --force for a demo commit")
        return

    buyer.ensure_key()
    buyer.shop()
    scout.service()
    buyer.follow_up()

    s = near.view("get_signal", {"signal_id": signal_id})
    wait((s["reveal_after_ms"] - now_ms()) / 1000 + 5, "signal horizon; buyers hold the signal exclusively")
    scout.service()
    verifier.step()
    buyer.follow_up()

    a = near.view("get_agent", {"account_id": scout.account})
    log("demo", f"{scout.account} record: {a['stats']} hit_rate_bps={a['hit_rate_bps']} reveal_rate_bps={a['reveal_rate_bps']}")


if __name__ == "__main__":
    main()
