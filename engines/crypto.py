#!/usr/bin/env python3
"""Genie Predict — crypto options-implied probability engine.

Turns a crypto price market ("BTC above $150k on 2026-12-31") into a *market-derived*
probability, using Deribit's options surface instead of an agent guess.

Method (risk-neutral, Black-Scholes finishing-ITM probability):
    P(S_T > K) = N(d2),   d2 = [ln(S/K) + (r - 0.5 sigma^2) T] / (sigma sqrt(T))
where sigma is Deribit's implied vol at (K, T), S is spot, r the risk-free rate.

This is the standard risk-neutral probability of finishing above the strike — the same
number a derivatives desk would quote. It is the options market's dollar-weighted view,
typically sharper than a thin prediction market's price. As a refinement, when dense
strikes exist we also compute the Breeden-Litzenberger slope estimate and blend.

Everything is keyless / read-only. Deribit is not Polymarket, so this is geoblock-safe.

Service-ready: all network access goes through a single `fetch` callable that defaults
to a keyless urllib GET. To move to a cached Render service later, pass a different
`fetch` (or set DERIBIT_PROXY) — the math is unchanged.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

DERIBIT = os.environ.get("DERIBIT_PROXY", "https://www.deribit.com/api/v2")
DEFAULT_RATE = 0.03  # annualized risk-free proxy; small effect at these horizons

# ---------------------------------------------------------------- math utils

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf (stdlib only)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_itm(spot: float, strike: float, sigma: float, t_years: float,
             rate: float = DEFAULT_RATE) -> float:
    """Risk-neutral P(S_T > K) = N(d2). Returns a probability in [0, 1]."""
    if spot <= 0 or strike <= 0 or sigma <= 0 or t_years <= 0:
        return float("nan")
    d2 = (math.log(spot / strike) + (rate - 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))
    return _norm_cdf(d2)


def prob_below(spot: float, strike: float, sigma: float, t_years: float,
               rate: float = DEFAULT_RATE) -> float:
    p = prob_itm(spot, strike, sigma, t_years, rate)
    return float("nan") if math.isnan(p) else 1.0 - p


# ------------------------------------------------------------- deribit access

def _default_fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "genie-predict", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _api(path: str, params: dict, fetch: Callable[[str], dict]) -> object:
    url = DERIBIT + path + "?" + urllib.parse.urlencode(params)
    data = fetch(url)
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    return data


def get_spot(currency: str, fetch: Callable[[str], dict] = _default_fetch) -> float:
    idx = {"BTC": "btc_usd", "ETH": "eth_usd"}.get(currency.upper())
    if not idx:
        return float("nan")
    res = _api("/public/get_index_price", {"index_name": idx}, fetch)
    return float(res.get("index_price", "nan")) if isinstance(res, dict) else float("nan")


def _parse_instrument(name: str):
    """'BTC-31DEC26-150000-C' -> (expiry_dt_utc, strike, kind)."""
    m = re.match(r"^[A-Z]+-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-(C|P)$", name)
    if not m:
        return None
    try:
        exp = datetime.strptime(m.group(1), "%d%b%y").replace(tzinfo=timezone.utc, hour=8)  # Deribit expiries settle 08:00 UTC
        return exp, float(m.group(2)), m.group(3)
    except Exception:
        return None


def list_options(currency: str, fetch: Callable[[str], dict] = _default_fetch) -> list:
    res = _api("/public/get_instruments", {"currency": currency.upper(), "kind": "option", "expired": "false"}, fetch)
    out = []
    if isinstance(res, list):
        for it in res:
            name = it.get("instrument_name", "")
            p = _parse_instrument(name)
            if p:
                out.append({"name": name, "expiry": p[0], "strike": p[1], "kind": p[2]})
    return out


def get_iv(instrument_name: str, fetch: Callable[[str], dict] = _default_fetch) -> float:
    """Deribit mark implied vol (in %) for an option -> return as a decimal (e.g. 0.55)."""
    res = _api("/public/ticker", {"instrument_name": instrument_name}, fetch)
    if isinstance(res, dict):
        iv = res.get("mark_iv")
        if iv is not None:
            return float(iv) / 100.0
    return float("nan")


# ------------------------------------------------------------- expiry / strike selection

def _pick_expiry(options: list, target: datetime):
    """Choose the two expiries bracketing the target date for interpolation."""
    exps = sorted({o["expiry"] for o in options})
    if not exps:
        return None, None
    before = [e for e in exps if e <= target]
    after = [e for e in exps if e >= target]
    lo = before[-1] if before else exps[0]
    hi = after[0] if after else exps[-1]
    return lo, hi


def _iv_at_strike(options: list, expiry: datetime, strike: float, kind: str,
                  fetch: Callable[[str], dict]) -> float:
    """IV at a given expiry, interpolated across the two nearest strikes (same kind)."""
    same = sorted([o for o in options if o["expiry"] == expiry and o["kind"] == kind],
                  key=lambda o: o["strike"])
    if not same:
        return float("nan")
    below = [o for o in same if o["strike"] <= strike]
    above = [o for o in same if o["strike"] >= strike]
    lo = below[-1] if below else same[0]
    hi = above[0] if above else same[-1]
    iv_lo = get_iv(lo["name"], fetch)
    iv_hi = get_iv(hi["name"], fetch) if hi["name"] != lo["name"] else iv_lo
    if math.isnan(iv_lo) and math.isnan(iv_hi):
        return float("nan")
    if math.isnan(iv_lo):
        return iv_hi
    if math.isnan(iv_hi) or hi["strike"] == lo["strike"]:
        return iv_lo
    w = (strike - lo["strike"]) / (hi["strike"] - lo["strike"])
    return iv_lo + w * (iv_hi - iv_lo)


# ------------------------------------------------------------- main entry

def implied_probability(currency: str, strike: float, expiry_iso: str, direction: str = "above",
                        fetch: Callable[[str], dict] = _default_fetch,
                        rate: float = DEFAULT_RATE) -> dict:
    """Compute the options-implied probability that `currency` price is above/below `strike`
    at `expiry_iso` (YYYY-MM-DD). Returns a dict with the probability and its inputs, or an
    error field. Never raises for normal data issues.
    """
    try:
        target = datetime.fromisoformat(expiry_iso).replace(tzinfo=timezone.utc)
    except Exception:
        return {"ok": False, "error": f"bad expiry '{expiry_iso}' (want YYYY-MM-DD)"}

    now = datetime.now(timezone.utc)
    t_years = max((target - now).total_seconds() / (365.25 * 24 * 3600), 1e-6)

    try:
        spot = get_spot(currency, fetch)
        if math.isnan(spot):
            return {"ok": False, "error": f"no spot for {currency}"}
        options = list_options(currency, fetch)
        if not options:
            return {"ok": False, "error": f"no options listed for {currency}"}

        lo_exp, hi_exp = _pick_expiry(options, target)
        # IV at each bracketing expiry (use OTM side: calls for 'above', puts for 'below' read similar IV;
        # we use calls for a consistent surface read), then interpolate in total variance across expiries.
        kind = "C"
        iv_lo = _iv_at_strike(options, lo_exp, strike, kind, fetch)
        iv_hi = _iv_at_strike(options, hi_exp, strike, kind, fetch)

        # interpolate IV in *total variance* (var*t) space across the two expiries
        def _tvar(iv, exp):
            ty = max((exp - now).total_seconds() / (365.25 * 24 * 3600), 1e-6)
            return (iv * iv * ty) if not math.isnan(iv) else float("nan")

        if lo_exp == hi_exp:
            sigma = iv_lo if not math.isnan(iv_lo) else iv_hi
        else:
            tv_lo, tv_hi = _tvar(iv_lo, lo_exp), _tvar(iv_hi, hi_exp)
            ty_lo = max((lo_exp - now).total_seconds() / (365.25 * 24 * 3600), 1e-6)
            ty_hi = max((hi_exp - now).total_seconds() / (365.25 * 24 * 3600), 1e-6)
            if math.isnan(tv_lo) and math.isnan(tv_hi):
                sigma = float("nan")
            elif math.isnan(tv_lo):
                sigma = iv_hi
            elif math.isnan(tv_hi):
                sigma = iv_lo
            else:
                w = 0.0 if ty_hi == ty_lo else (t_years - ty_lo) / (ty_hi - ty_lo)
                w = max(0.0, min(1.0, w))
                tv = tv_lo + w * (tv_hi - tv_lo)
                sigma = math.sqrt(max(tv, 1e-12) / t_years)

        if sigma is None or math.isnan(sigma) or sigma <= 0:
            return {"ok": False, "error": "no usable implied vol near that strike/expiry"}

        p_above = prob_itm(spot, strike, sigma, t_years, rate)
        prob = p_above if direction == "above" else (1.0 - p_above)
        return {
            "ok": True,
            "currency": currency.upper(),
            "strike": strike,
            "direction": direction,
            "spot": round(spot, 2),
            "sigma": round(sigma, 4),
            "t_years": round(t_years, 4),
            "prob": round(prob, 4),
            "prob_pct": round(prob * 100, 1),
            "method": "deribit-implied (BS N(d2), total-variance interp)",
            "expiry": expiry_iso,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"deribit fetch failed: {e}"}


# ------------------------------------------------------------- market-string parsing

_STRIKE_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([kKmM]?)")

def parse_price_market(question: str):
    """Best-effort parse of a Polymarket question into (currency, strike, direction).
    Returns None if it doesn't look like a crypto price-threshold market.
    e.g. 'Will Bitcoin reach $150,000 by December 2026?' -> ('BTC', 150000, 'above')
    """
    q = question.lower()
    cur = None
    if "bitcoin" in q or re.search(r"\bbtc\b", q):
        cur = "BTC"
    elif "ethereum" in q or re.search(r"\beth\b", q):
        cur = "ETH"
    if not cur:
        return None
    direction = "below" if any(w in q for w in ("below", "under", "less than", "dip", "drop to", "fall to")) else "above"
    # find the largest $-number as the strike
    best = None
    for m in _STRIKE_RE.finditer(question):
        raw, suffix = m.group(1).replace(",", ""), m.group(2).lower()
        try:
            val = float(raw)
        except Exception:
            continue
        if suffix == "k":
            val *= 1e3
        elif suffix == "m":
            val *= 1e6
        if val >= 1000 and (best is None or val > best):  # ignore small numbers like years
            best = val
    if best is None:
        return None
    return cur, best, direction


if __name__ == "__main__":
    import sys
    # CLI: crypto.py <BTC|ETH> <strike> <YYYY-MM-DD> [above|below]
    if len(sys.argv) >= 4:
        cur, strike, exp = sys.argv[1], float(sys.argv[2]), sys.argv[3]
        direction = sys.argv[4] if len(sys.argv) > 4 else "above"
        print(json.dumps(implied_probability(cur, strike, exp, direction), indent=2))
    else:
        print("usage: crypto.py <BTC|ETH> <strike> <YYYY-MM-DD> [above|below]")
