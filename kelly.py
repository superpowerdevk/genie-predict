#!/usr/bin/env python3
"""Genie Predict — Kelly sizing for binary prediction-market bets.

Given your probability `p` and the market price `price` (both 0..1), the Kelly-optimal
fraction of bankroll for a binary market that pays $1 per $price staked is:

    edge = p - price
    f*   = edge / (1 - price)          # buying YES at `price`
    (for the NO side, mirror with p'=1-p, price'=1-price)

We report a FRACTIONAL Kelly (default quarter) because full Kelly is famously too
aggressive for real bankrolls and model error. Output is a suggested % of bankroll —
a sizing guide, never advice.
"""
from __future__ import annotations


def kelly(p: float, price: float, fraction: float = 0.25) -> dict:
    """Return sizing for the value side.
    p      = your probability the market resolves YES (0..1)
    price  = current YES price on the market (0..1)
    fraction = Kelly fraction to apply (0.25 = quarter Kelly)
    """
    try:
        p = float(p); price = float(price)
    except Exception:
        return {"ok": False, "error": "bad inputs"}
    if not (0 < price < 1) or not (0 <= p <= 1):
        return {"ok": False, "error": "p and price must be in (0,1)"}

    # decide the value side
    if p >= price:
        side = "YES"
        edge = p - price
        f_full = edge / (1.0 - price)          # payout if YES: (1-price)/price per $ -> Kelly simplifies
    else:
        side = "NO"
        p_no, price_no = 1.0 - p, 1.0 - price
        edge = p_no - price_no
        f_full = edge / (1.0 - price_no)

    f_full = max(0.0, f_full)
    f = max(0.0, min(1.0, f_full * fraction))
    return {
        "ok": True,
        "side": side,
        "edge_pts": round((p - price) * 100, 1),
        "kelly_full_pct": round(f_full * 100, 1),
        "fraction": fraction,
        "pct_bankroll": round(f * 100, 1),
    }


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) >= 3:
        p = float(sys.argv[1]); price = float(sys.argv[2])
        frac = float(sys.argv[3]) if len(sys.argv) > 3 else 0.25
        # accept percentages too
        if p > 1: p /= 100.0
        if price > 1: price /= 100.0
        print(json.dumps(kelly(p, price, frac), indent=2))
    else:
        print("usage: kelly.py <your_prob> <market_price> [fraction]  (probs as 0..1 or 0..100)")
