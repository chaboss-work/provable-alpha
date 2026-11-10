"""Price snapshots: NEAR Intents (1Click dry quotes) vs centralized exchange order books.

A snapshot compares the price you would actually get swapping through NEAR Intents with the
mid price on Binance, OKX and Bybit. The deviation of the Intents mid from the CEX mid
(`dev_bps`) is what the scout trades on: large deviations tend to close as solvers rebalance.
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import asdict, dataclass

import requests

UA = {"user-agent": "provable-alpha-agent/0.1"}
ONECLICK = "https://1click.chaindefuser.com/v0/quote"
WNEAR = "nep141:wrap.near"
USDT = "nep141:usdt.tether-token.near"
NEAR_DECIMALS = 24
USDT_DECIMALS = 6


@dataclass
class Snapshot:
    observed_at: str
    size_near: float
    cex: dict            # venue -> {"bid": float, "ask": float}
    cex_mid: float
    best_bid: float
    best_ask: float
    intents_sell_px: float   # USDT received per NEAR when selling size_near through Intents
    intents_buy_px: float    # USDT paid per NEAR when buying ~size_near through Intents
    intents_mid: float
    dev_bps: float           # (intents_mid / cex_mid - 1) * 1e4
    edge_sell_bps: float     # (intents_sell_px / best_ask - 1) * 1e4: >0 means sell on Intents, buy on CEX
    edge_buy_bps: float      # (best_bid / intents_buy_px - 1) * 1e4: >0 means buy on Intents, sell on CEX
    quote_ids: list

    def to_dict(self) -> dict:
        return asdict(self)


def _get(url: str) -> dict:
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    return r.json()


def cex_books() -> dict:
    books = {}
    try:
        d = _get("https://api.binance.com/api/v3/ticker/bookTicker?symbol=NEARUSDT")
        books["binance"] = {"bid": float(d["bidPrice"]), "ask": float(d["askPrice"])}
    except Exception:
        pass
    try:
        d = _get("https://www.okx.com/api/v5/market/ticker?instId=NEAR-USDT")["data"][0]
        books["okx"] = {"bid": float(d["bidPx"]), "ask": float(d["askPx"])}
    except Exception:
        pass
    try:
        d = _get("https://api.bybit.com/v5/market/tickers?category=spot&symbol=NEARUSDT")["result"]["list"][0]
        books["bybit"] = {"bid": float(d["bid1Price"]), "ask": float(d["ask1Price"])}
    except Exception:
        pass
    if len(books) < 2:
        raise RuntimeError(f"need at least 2 CEX books, got {list(books)}")
    return books


def _quote(origin: str, dest: str, amount: int) -> dict:
    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    body = {
        "dry": True, "swapType": "EXACT_INPUT", "slippageTolerance": 100,
        "originAsset": origin, "depositType": "INTENTS",
        "destinationAsset": dest, "amount": str(amount),
        "refundTo": "intents.near", "refundType": "INTENTS",
        "recipient": "intents.near", "recipientType": "INTENTS", "deadline": deadline,
    }
    r = requests.post(ONECLICK, json=body, headers={**UA, "content-type": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json()


def intents_prices(size_near: float, ref_price: float) -> tuple[float, float, list]:
    sell = _quote(WNEAR, USDT, int(size_near * 10**NEAR_DECIMALS))
    sell_px = int(sell["quote"]["amountOut"]) / 10**USDT_DECIMALS / size_near
    usdt_in = round(size_near * ref_price, 2)
    buy = _quote(USDT, WNEAR, int(usdt_in * 10**USDT_DECIMALS))
    near_out = int(buy["quote"]["amountOut"]) / 10**NEAR_DECIMALS
    buy_px = usdt_in / near_out
    return sell_px, buy_px, [sell.get("correlationId"), buy.get("correlationId")]


def build_snapshot(books: dict, sell_px: float, buy_px: float, size_near: float, quote_ids: list, observed_at: str) -> Snapshot:
    mids = [(b["bid"] + b["ask"]) / 2 for b in books.values()]
    cex_mid = statistics.median(mids)
    best_bid = max(b["bid"] for b in books.values())
    best_ask = min(b["ask"] for b in books.values())
    intents_mid = (sell_px + buy_px) / 2
    return Snapshot(
        observed_at=observed_at,
        size_near=size_near,
        cex=books,
        cex_mid=round(cex_mid, 6),
        best_bid=best_bid,
        best_ask=best_ask,
        intents_sell_px=round(sell_px, 6),
        intents_buy_px=round(buy_px, 6),
        intents_mid=round(intents_mid, 6),
        dev_bps=round((intents_mid / cex_mid - 1) * 1e4, 2),
        edge_sell_bps=round((sell_px / best_ask - 1) * 1e4, 2),
        edge_buy_bps=round((best_bid / buy_px - 1) * 1e4, 2),
        quote_ids=quote_ids,
    )


def snapshot(size_near: float = 100.0) -> Snapshot:
    books = cex_books()
    ref = statistics.median([(b["bid"] + b["ask"]) / 2 for b in books.values()])
    sell_px, buy_px, ids = intents_prices(size_near, ref)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return build_snapshot(books, sell_px, buy_px, size_near, ids, now)
