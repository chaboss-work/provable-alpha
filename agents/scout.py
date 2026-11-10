"""Seller agent: watches NEAR Intents vs CEX prices, commits signals, delivers them sealed
to buyers, and reveals them publicly after the horizon.

    python scout.py                 # run forever
    python scout.py --once          # one measurement + service pass
"""
from __future__ import annotations

import argparse
import time

from pa import market, near, sealing, strategy
from pa.util import load_state, log, now_ms, save_state

ACCOUNT = "scout.provable-alpha.testnet"


class Scout:
    def __init__(self, account: str = ACCOUNT, price_near: str = "0.01", bond_near: str = "0.05",
                 threshold_bps: float = 6.0, horizon_s: int = 600, reveal_grace_s: int = 1800,
                 size_near: float = 100.0, max_open: int = 2, min_gap_s: int = 0):
        self.account = account
        self.price = near.to_yocto(price_near)
        self.bond = near.to_yocto(bond_near)
        self.threshold = threshold_bps
        self.horizon_s = horizon_s
        self.reveal_grace_s = reveal_grace_s
        self.size = size_near
        self.max_open = max_open
        self.min_gap_s = min_gap_s
        self.state = load_state("scout")
        self.state.setdefault("signals", {})
        b = self.state.get("baseline") or {}
        self.baseline = strategy.Baseline(value=b.get("value"), samples=b.get("samples", 0), history=b.get("history", []))
        cfg = near.view("get_config")
        self.storage_fee = int(cfg["storage_fee"])

    def save(self) -> None:
        self.state["baseline"] = {"value": self.baseline.value, "samples": self.baseline.samples, "history": self.baseline.history}
        save_state("scout", self.state)

    def open_signals(self) -> dict:
        return {k: v for k, v in self.state["signals"].items() if not v.get("revealed")}

    # -- measure and commit --
    def measure(self, forced: bool = False) -> int | None:
        snap = market.snapshot(self.size)
        payload = None
        gap_ok = now_ms() - self.state.get("last_commit_ms", 0) >= self.min_gap_s * 1000
        if len(self.open_signals()) < self.max_open and (gap_ok or forced):
            payload = strategy.make_signal(snap, self.baseline, self.threshold, self.horizon_s, forced=forced)
        self.baseline.update(snap.dev_bps)
        log("scout", f"dev={snap.dev_bps:+.2f}bps baseline={self.baseline.value:+.2f} (n={self.baseline.samples})")
        signal_id = None
        if payload:
            signal_id = self.commit(payload)
        self.save()
        return signal_id

    def commit(self, payload_obj: dict) -> int:
        payload = sealing.canonical(payload_obj)
        salt = sealing.new_salt()
        commitment = sealing.commitment_of(payload, salt)
        reveal_after = now_ms() + self.horizon_s * 1000
        deadline = reveal_after + self.reveal_grace_s * 1000
        r = near.call(self.account, "commit_signal", {
            "market": payload_obj["market"],
            "commitment": commitment,
            "price": str(self.price),
            "reveal_after_ms": reveal_after,
            "reveal_deadline_ms": deadline,
        }, deposit_yocto=self.bond + self.storage_fee)
        signal_id = r["value"]
        if signal_id is None:  # fall back to the newest signal by this seller
            latest = near.view("get_signals", {"from_index": 0, "limit": 5})
            signal_id = next(s["id"] for s in latest if s["seller"] == self.account and s["commitment"] == commitment)
        self.state["last_commit_ms"] = now_ms()
        self.state["signals"][str(signal_id)] = {
            "payload": payload, "salt": salt, "commitment": commitment,
            "reveal_after_ms": reveal_after, "deadline_ms": deadline, "delivered": [], "revealed": False,
        }
        log("scout", f"committed signal #{signal_id} {payload_obj['direction']} excess={payload_obj['excess_bps']:+.2f}bps "
                     f"commitment={commitment[:16]}…", r["tx"])
        return signal_id

    # -- deliver and reveal --
    def service(self) -> None:
        for sid, s in list(self.open_signals().items()):
            signal_id = int(sid)
            for p in near.view("get_purchases", {"signal_id": signal_id}) or []:
                buyer = p["buyer"]
                if p["delivered_at_ms"] or p["refunded"] or buyer in s["delivered"]:
                    continue
                key = near.view("get_encryption_key", {"account_id": buyer})
                ciphertext = sealing.seal(s["payload"], s["salt"], key)
                r = near.call(self.account, "deliver", {"signal_id": signal_id, "buyer": buyer, "ciphertext": ciphertext})
                s["delivered"].append(buyer)
                log("scout", f"delivered #{signal_id} to {buyer} (sealed to its X25519 key, payment released)", r["tx"])
            if now_ms() >= s["reveal_after_ms"] + 3000:
                onchain = near.view("get_signal", {"signal_id": signal_id})
                if onchain and onchain["status"] == "Open":
                    r = near.call(self.account, "reveal_signal", {"signal_id": signal_id, "payload": s["payload"], "salt": s["salt"]})
                    log("scout", f"revealed #{signal_id} publicly; contract checked sha256(payload:salt) == commitment, bond returned", r["tx"])
                s["revealed"] = True
            self.save()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--threshold", type=float, default=6.0)
    ap.add_argument("--horizon", type=int, default=600)
    args = ap.parse_args()
    scout = Scout(threshold_bps=args.threshold, horizon_s=args.horizon)
    while True:
        try:
            scout.measure()
            scout.service()
        except Exception as e:  # keep the agent alive; errors are logged
            log("scout", f"error: {e}")
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
