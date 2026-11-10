"""Minimal NEAR access for the agents.

Views go straight to JSON-RPC. Transactions are signed by near-cli-rs with keys from the
local legacy keychain (~/.near-credentials), so private keys never pass through Python.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
from decimal import Decimal

import requests

NETWORK = os.environ.get("PA_NETWORK", "testnet")
_DEFAULT_RPCS = {
    "testnet": ["https://test.rpc.fastnear.com", "https://rpc.testnet.near.org"],
    "mainnet": ["https://free.rpc.fastnear.com", "https://rpc.mainnet.near.org"],
}
RPC_URLS = [u for u in os.environ.get("PA_RPC", "").split(",") if u] or _DEFAULT_RPCS[NETWORK]
RPC_URL = RPC_URLS[0]
CONTRACT = os.environ.get("PA_CONTRACT", "market.provable-alpha.testnet")
NEAR_CLI = os.environ.get("PA_NEAR_CLI") or shutil.which("near") or "near"
YOCTO = 10**24


def to_yocto(near: str | Decimal | float) -> int:
    return int(Decimal(str(near)) * YOCTO)


def from_yocto(yocto: int | str) -> Decimal:
    return Decimal(int(yocto)) / YOCTO


class NearError(RuntimeError):
    pass


def rpc(method: str, params: dict) -> dict:
    """JSON-RPC with fallback across endpoints and backoff on rate limits."""
    last = None
    for attempt in range(6):
        url = RPC_URLS[attempt % len(RPC_URLS)]
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": "pa", "method": method, "params": params}, timeout=30)
            if r.status_code in (429, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(min(2 ** attempt, 20))
            continue
        if "error" in data:
            raise NearError(json.dumps(data["error"])[:500])
        return data["result"]
    raise NearError(f"RPC unavailable: {last}")


def view(method: str, args: dict | None = None, contract: str = CONTRACT):
    result = rpc(
        "query",
        {
            "request_type": "call_function",
            "finality": "optimistic",
            "account_id": contract,
            "method_name": method,
            "args_base64": base64.b64encode(json.dumps(args or {}).encode()).decode(),
        },
    )
    if "error" in result:
        raise NearError(result["error"])
    raw = bytes(result["result"]).decode()
    return json.loads(raw) if raw else None


def balance(account_id: str) -> Decimal:
    result = rpc("query", {"request_type": "view_account", "finality": "final", "account_id": account_id})
    return from_yocto(result["amount"])


_RETURN_RE = re.compile(r"Function execution return value \(printed to stdout\):\s*(.*)", re.S)


def call(signer: str, method: str, args: dict, deposit_yocto: int = 0, gas_tgas: int = 100, contract: str = CONTRACT):
    """Send a function-call transaction signed with `signer`'s keychain key. Returns the decoded return value."""
    cmd = [
        NEAR_CLI, "contract", "call-function", "as-transaction", contract, method,
        "json-args", json.dumps(args, separators=(",", ":")),
        "prepaid-gas", f"{gas_tgas} Tgas",
        "attached-deposit", f"{deposit_yocto} yoctoNEAR",
        "sign-as", signer,
        "network-config", NETWORK,
        "sign-with-legacy-keychain", "send",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    out = proc.stdout.strip()
    err = proc.stderr
    if proc.returncode != 0:
        msg = re.findall(r"(?:Smart contract panicked|ExecutionError|Error)[^\n]*", err + "\n" + out)
        raise NearError(f"{method} failed: {msg[-1] if msg else (err or out)[-400:]}")
    tx = re.search(r"Transaction ID: (\w+)", err + out)
    value = None
    if out:
        try:
            value = json.loads(out.splitlines()[-1])
        except json.JSONDecodeError:
            value = None
    return {"tx": tx.group(1) if tx else None, "value": value}
