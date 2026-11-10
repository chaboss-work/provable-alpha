from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
EXPLORER = "https://testnet.nearblocks.io/txns/"


def now_ms() -> int:
    return int(time.time() * 1000)


def load_state(name: str) -> dict:
    p = STATE_DIR / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_state(name: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"{name}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(p)


def log(agent: str, msg: str, tx: str | None = None) -> None:
    t = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    line = f"[{t}] {agent:<9} {msg}"
    if tx:
        line += f"  {EXPLORER}{tx}"
    print(line, flush=True)
