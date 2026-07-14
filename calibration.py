#!/usr/bin/env python3
"""Genie Predict — calibration & track-record engine.

Logs every forecast, records outcomes when markets resolve, and scores whether the
skill is actually *calibrated* — the thing that turns "trust me" into "here's my record."

Metrics:
  - Brier score  = mean((p - outcome)^2). Lower is better. Compared head-to-head vs the
    market's own Brier, this answers: is Genie's number sharper than the price?
  - Calibration curve: bucket forecasts by predicted probability and compare predicted
    vs realized frequency ("when I say 70%, it happens ~X%").
  - Edge realized: following "my read" vs following the market — did the skill add value?

Storage is behind a swappable interface (the point of the service-ready design):
  - default = local JSON at ~/.genie-predict/forecasts.json
  - later   = a RenderStore hitting an HTTP endpoint, SAME interface, 1-line swap.
calibration.py never knows which store it's using.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

# ------------------------------------------------------------- storage interface

class Store:
    """Abstract forecast store. Implement get_all / append / update to back it with
    anything (local file, Render endpoint, Postgres). calibration.py depends only on this."""

    def get_all(self) -> list:
        raise NotImplementedError

    def append(self, record: dict) -> None:
        raise NotImplementedError

    def update(self, forecast_id: str, patch: dict) -> bool:
        raise NotImplementedError


class LocalJSONStore(Store):
    """Default store: a JSON array on disk. Best-effort; never raises on a read-only runtime."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.expanduser("~/.genie-predict/forecasts.json")

    def _read(self) -> list:
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _write(self, data: list) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception:
            return False

    def get_all(self) -> list:
        return self._read()

    def append(self, record: dict) -> None:
        data = self._read()
        data.append(record)
        self._write(data)

    def update(self, forecast_id: str, patch: dict) -> bool:
        data = self._read()
        hit = False
        for rec in data:
            if rec.get("id") == forecast_id:
                rec.update(patch)
                hit = True
        if hit:
            self._write(data)
        return hit


# A module-level default store; swap this for a RenderStore to go service-backed.
_STORE: Store = LocalJSONStore()

def set_store(store: Store) -> None:
    global _STORE
    _STORE = store


# ------------------------------------------------------------- logging

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_id(slug: str) -> str:
    return f"{slug}:{int(time.time())}"


def log_forecast(slug: str, question: str, my_prob: float, market_prob: float,
                 direction: str = "yes", resolve_date: str = "", method: str = "agent",
                 store: Optional[Store] = None) -> str:
    """Record a forecast at the moment it's made. Probabilities are 0..1.
    Returns the forecast id (stable, for later resolution)."""
    st = store or _STORE
    fid = make_id(slug)
    st.append({
        "id": fid,
        "slug": slug,
        "question": question,
        "my_prob": round(float(my_prob), 4),
        "market_prob": round(float(market_prob), 4),
        "direction": direction,
        "resolve_date": resolve_date,
        "method": method,            # 'deribit-implied' | 'agent' | 'line-divergence' | ...
        "logged_at": _now_iso(),
        "outcome": None,             # filled by resolve_forecast: 1 (happened) / 0 (didn't)
        "resolved_at": None,
    })
    return fid


def resolve_forecast(forecast_id: str, outcome: int, store: Optional[Store] = None) -> bool:
    """Record the real outcome (1 = the forecast side happened, 0 = it didn't)."""
    st = store or _STORE
    return st.update(forecast_id, {"outcome": int(bool(outcome)), "resolved_at": _now_iso()})


# ------------------------------------------------------------- scoring

def _brier(pairs) -> float:
    """pairs = list of (prob, outcome). Returns mean squared error."""
    if not pairs:
        return float("nan")
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def score(store: Optional[Store] = None) -> dict:
    """Compute the full track record over all *resolved* forecasts."""
    st = store or _STORE
    all_f = st.get_all()
    resolved = [f for f in all_f if f.get("outcome") in (0, 1)]
    n = len(resolved)
    if n == 0:
        return {
            "ok": True, "n_total": len(all_f), "n_resolved": 0,
            "message": "No resolved forecasts yet — track record builds as markets settle.",
        }

    my_pairs = [(f["my_prob"], f["outcome"]) for f in resolved]
    mkt_pairs = [(f["market_prob"], f["outcome"]) for f in resolved]
    my_brier = _brier(my_pairs)
    mkt_brier = _brier(mkt_pairs)

    # hit rate: did the higher-probability side match the outcome?
    my_hits = sum(1 for f in resolved if (f["my_prob"] >= 0.5) == bool(f["outcome"]))
    mkt_hits = sum(1 for f in resolved if (f["market_prob"] >= 0.5) == bool(f["outcome"]))

    # edge realized: on the markets where we DISAGREED with the market direction, who was right?
    disagree = [f for f in resolved if (f["my_prob"] >= 0.5) != (f["market_prob"] >= 0.5)]
    my_right_on_disagree = sum(1 for f in disagree if (f["my_prob"] >= 0.5) == bool(f["outcome"]))

    # calibration curve: 10 buckets
    buckets = []
    for lo in range(0, 100, 10):
        hi = lo + 10
        inb = [f for f in resolved if lo <= f["my_prob"] * 100 < hi or (hi == 100 and f["my_prob"] == 1.0)]
        if inb:
            pred = sum(f["my_prob"] for f in inb) / len(inb)
            actual = sum(f["outcome"] for f in inb) / len(inb)
            buckets.append({"range": f"{lo}-{hi}%", "n": len(inb),
                            "predicted": round(pred * 100, 1), "actual": round(actual * 100, 1)})

    return {
        "ok": True,
        "n_total": len(all_f),
        "n_resolved": n,
        "brier_my": round(my_brier, 4),
        "brier_market": round(mkt_brier, 4),
        "beats_market": my_brier < mkt_brier,
        "brier_edge": round(mkt_brier - my_brier, 4),   # positive = we're sharper than the price
        "hit_rate_my": round(my_hits / n * 100, 1),
        "hit_rate_market": round(mkt_hits / n * 100, 1),
        "n_disagreed": len(disagree),
        "right_when_disagreed_pct": round(my_right_on_disagree / len(disagree) * 100, 1) if disagree else None,
        "calibration": buckets,
    }


def render_scorecard(store: Optional[Store] = None) -> str:
    """A compact markdown scorecard for the board footer / a dedicated screen."""
    s = score(store)
    if not s.get("n_resolved"):
        return ("📊 **Track record** · _building — no markets have resolved yet. "
                "Every forecast is logged and scored the moment its market settles._")
    beat = "🟢 beats the market" if s["beats_market"] else "🔴 trails the market"
    lines = [
        f"📊 **Track record** · {s['n_resolved']} resolved forecasts",
        f"- Brier **{s['brier_my']}** vs market **{s['brier_market']}** — {beat} "
        f"({'+' if s['brier_edge']>=0 else ''}{s['brier_edge']})",
        f"- Directional hit rate: **{s['hit_rate_my']}%** (market {s['hit_rate_market']}%)",
    ]
    if s.get("right_when_disagreed_pct") is not None:
        lines.append(f"- When Genie disagreed with the price ({s['n_disagreed']}×): "
                     f"**right {s['right_when_disagreed_pct']}%** of the time")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scorecard"
    if cmd == "scorecard":
        print(render_scorecard())
    elif cmd == "score":
        print(json.dumps(score(), indent=2))
    elif cmd == "log" and len(sys.argv) >= 6:
        # log <slug> <my_prob> <market_prob> <resolve_date>
        fid = log_forecast(sys.argv[2], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]),
                           resolve_date=sys.argv[5])
        print(f"logged {fid}")
    elif cmd == "resolve" and len(sys.argv) >= 4:
        ok = resolve_forecast(sys.argv[2], int(sys.argv[3]))
        print("resolved" if ok else "not found")
    else:
        print("usage: calibration.py scorecard|score|log <slug> <my> <mkt> <date>|resolve <id> <0|1>")
