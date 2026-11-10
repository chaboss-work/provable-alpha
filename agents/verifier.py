"""Verifier agent: after a signal is revealed, re-measures the market and records on-chain
whether the prediction held. Also expires signals whose seller missed the reveal deadline.

    python verifier.py         # run forever
    python verifier.py --once
"""
from __future__ import annotations

import argparse
import json
import time

from pa import market, near, strategy
from pa.util import log, now_ms

ACCOUNT = "verifier.provable-alpha.testnet"


class Verifier:
    def __init__(self, account: str = ACCOUNT):
        self.account = account

    def step(self) -> None:
        for s in near.view("get_signals", {"from_index": 0, "limit": 50}) or []:
            sid = s["id"]
            if s["status"] == "Revealed":
                payload = json.loads(s["payload"])
                after = market.snapshot(float(payload.get("size_near", 100)))
                hit, realized, evidence = strategy.judge(payload, after)
                r = near.call(self.account, "settle_signal", {
                    "signal_id": sid, "hit": hit, "realized_bps": int(round(realized)), "evidence": evidence,
                })
                log("verifier", f"settled #{sid}: {'HIT' if hit else 'MISS'} (excess {payload['excess_bps']:+.2f} -> "
                                f"{after.dev_bps - payload['baseline_bps']:+.2f} bps, captured {realized:+.2f} bps)", r["tx"])
            elif s["status"] == "Open" and now_ms() > s["reveal_deadline_ms"] + 5000:
                r = near.call(self.account, "expire_signal", {"signal_id": sid})
                log("verifier", f"expired #{sid}: seller missed the reveal, bond goes to buyers", r["tx"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()
    v = Verifier()
    while True:
        try:
            v.step()
        except Exception as e:
            log("verifier", f"error: {e}")
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
