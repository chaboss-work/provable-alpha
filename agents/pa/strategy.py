"""Signal logic of the spread scout and the verifier's judging rule.

The Intents mid trades at a structural offset to the CEX mid (fees, solver inventory).
The scout tracks that offset with an EWMA baseline and fires when the current deviation
departs from it by more than `threshold_bps`. The prediction is mean reversion: within
`horizon_s` the excess deviation shrinks by at least half or flips sign.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .market import Snapshot

AGENT = "spread-scout/0.1"


@dataclass
class Baseline:
    alpha: float = 0.2
    min_samples: int = 3
    value: float | None = None
    samples: int = 0
    history: list = field(default_factory=list)

    def update(self, x: float) -> None:
        self.value = x if self.value is None else self.alpha * x + (1 - self.alpha) * self.value
        self.samples += 1
        self.history = (self.history + [round(x, 2)])[-20:]

    @property
    def ready(self) -> bool:
        return self.samples >= self.min_samples


def make_signal(snap: Snapshot, baseline: Baseline, threshold_bps: float, horizon_s: int, forced: bool = False) -> dict | None:
    """Return a payload dict, or None if there is no signal."""
    if not baseline.ready or baseline.value is None:
        return None
    excess = snap.dev_bps - baseline.value
    if abs(excess) < threshold_bps and not forced:
        return None
    direction = "intents_rich" if excess > 0 else "intents_cheap"
    return {
        "v": 1,
        "agent": AGENT,
        "market": "NEAR/USDT",
        "type": "intents_cex_reversion",
        "direction": direction,
        "observed_at": snap.observed_at,
        "size_near": snap.size_near,
        "cex": snap.cex,
        "cex_mid": snap.cex_mid,
        "intents_sell_px": snap.intents_sell_px,
        "intents_buy_px": snap.intents_buy_px,
        "dev_bps": snap.dev_bps,
        "baseline_bps": round(baseline.value, 2),
        "excess_bps": round(excess, 2),
        "threshold_bps": threshold_bps,
        "horizon_s": horizon_s,
        "prediction": "abs(dev_bps - baseline_bps) at horizon <= 0.5 * abs(excess_bps), or the sign flips",
        "action_hint": (
            "buy NEAR via Intents, sell on CEX" if direction == "intents_cheap" else "buy NEAR on CEX, sell via Intents"
        ),
        "forced": forced,
        "quote_ids": snap.quote_ids,
    }


def judge(payload: dict, after: Snapshot) -> tuple[bool, float, str]:
    """Verifier rule. Returns (hit, realized_bps, evidence)."""
    base = float(payload["baseline_bps"])
    before = float(payload["excess_bps"])
    excess_after = after.dev_bps - base
    flipped = (before > 0) != (excess_after > 0) and abs(excess_after) > 0
    hit = abs(excess_after) <= 0.5 * abs(before) or flipped
    # Move of the excess in the predicted direction (towards and through the baseline).
    predicted_sign = -1.0 if before > 0 else 1.0
    realized = predicted_sign * (excess_after - before)
    evidence = (
        f"excess_before={before:.2f}bps excess_after={excess_after:.2f}bps "
        f"dev_after={after.dev_bps:.2f} baseline={base:.2f} cex_mid={after.cex_mid} "
        f"intents_mid={after.intents_mid} at={after.observed_at} quotes={','.join(str(q) for q in after.quote_ids)}"
    )[:500]
    return hit, round(realized, 2), evidence
