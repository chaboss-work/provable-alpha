"""Buyer agent: buys signals from sellers with a good on-chain record, decrypts the sealed
delivery, checks it against the commitment, and later checks the public reveal.

    python buyer.py            # run forever
    python buyer.py --once
"""
from __future__ import annotations

import argparse
import json
import time

from pa import near, sealing
from pa.util import STATE_DIR, load_state, log, now_ms, save_state

ACCOUNT = "buyer.provable-alpha.testnet"


class Buyer:
    def __init__(self, account: str = ACCOUNT, max_price_near: str = "0.05", budget_near: str = "1.0",
                 min_reveal_rate_bps: int = 8000, min_hit_rate_bps: int = 4000, min_settled_for_hit_rate: int = 5):
        self.account = account
        self.max_price = near.to_yocto(max_price_near)
        self.budget = near.to_yocto(budget_near)
        self.min_reveal = min_reveal_rate_bps
        self.min_hit = min_hit_rate_bps
        self.min_settled = min_settled_for_hit_rate
        self.key = sealing.load_or_create_key(STATE_DIR / "buyer.key")
        self.state = load_state("buyer")
        self.state.setdefault("bought", {})
        self.state.setdefault("spent", 0)
        self.storage_fee = int(near.view("get_config")["storage_fee"])

    def save(self) -> None:
        save_state("buyer", self.state)

    def ensure_key(self) -> None:
        pub = sealing.public_b64(self.key)
        if near.view("get_encryption_key", {"account_id": self.account}) != pub:
            r = near.call(self.account, "set_encryption_key", {"key": pub})
            log("buyer", "registered X25519 key for sealed deliveries", r["tx"])

    def seller_ok(self, seller: str) -> tuple[bool, str]:
        a = near.view("get_agent", {"account_id": seller})
        if not a:
            return True, "new seller"
        st = a["stats"]
        if st["revealed"] + st["missed"] >= 3 and (a["reveal_rate_bps"] or 0) < self.min_reveal:
            return False, f"reveal rate {a['reveal_rate_bps']} bps"
        if st["hits"] + st["misses"] >= self.min_settled and (a["hit_rate_bps"] or 0) < self.min_hit:
            return False, f"hit rate {a['hit_rate_bps']} bps"
        hr = a["hit_rate_bps"]
        return True, f"record: {st['hits']} hits / {st['misses']} misses, reveal rate {a['reveal_rate_bps']} bps" if hr is not None else "no settled signals yet"

    def shop(self) -> None:
        for s in near.view("get_signals", {"from_index": 0, "limit": 20}) or []:
            sid = str(s["id"])
            if s["status"] != "Open" or sid in self.state["bought"] or s["seller"] == self.account:
                continue
            if now_ms() > s["reveal_after_ms"] - 20_000:
                continue
            price = int(s["price"])
            if price > self.max_price or self.state["spent"] + price > self.budget:
                continue
            ok, why = self.seller_ok(s["seller"])
            if not ok:
                log("buyer", f"skip #{sid} from {s['seller']}: {why}")
                continue
            r = near.call(self.account, "buy_signal", {"signal_id": s["id"]}, deposit_yocto=price + self.storage_fee)
            self.state["bought"][sid] = {"commitment": s["commitment"], "seller": s["seller"], "verified": False, "checked_reveal": False}
            self.state["spent"] += price
            log("buyer", f"bought #{sid} from {s['seller']} for {near.from_yocto(price)} NEAR ({why})", r["tx"])
            self.save()

    def follow_up(self) -> None:
        for sid, b in self.state["bought"].items():
            if not b["verified"]:
                p = near.view("get_purchase", {"signal_id": int(sid), "buyer": self.account})
                if p and p.get("ciphertext"):
                    payload, salt = sealing.open_sealed(p["ciphertext"], self.key)
                    ok = sealing.commitment_of(payload, salt) == b["commitment"]
                    b.update(verified=ok, payload=payload, salt=salt)
                    sig = json.loads(payload)
                    log("buyer", f"#{sid} decrypted; sha256(payload:salt) {'MATCHES' if ok else 'DOES NOT MATCH'} the on-chain commitment")
                    log("buyer", f"#{sid} signal: {sig['direction']} excess={sig['excess_bps']:+.2f}bps horizon={sig['horizon_s']}s -> paper trade: {sig['action_hint']}")
                elif p and p.get("refunded"):
                    b["verified"] = True
            elif not b["checked_reveal"] and b.get("payload"):
                s = near.view("get_signal", {"signal_id": int(sid)})
                if s and s.get("payload"):
                    same = s["payload"] == b["payload"]
                    b["checked_reveal"] = True
                    log("buyer", f"#{sid} public reveal {'equals' if same else 'DIFFERS FROM'} what was delivered privately")
            self.save()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()
    buyer = Buyer()
    buyer.ensure_key()
    while True:
        try:
            buyer.shop()
            buyer.follow_up()
        except Exception as e:
            log("buyer", f"error: {e}")
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
