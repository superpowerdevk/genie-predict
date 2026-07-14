"""Regression tests for the crypto options-implied engine. Run: python3 tests/test_crypto.py"""
import sys, os, math, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engines import crypto
from datetime import datetime, timezone

def approx(a, b, tol=0.005): return abs(a-b) < tol

# 1. N(d2) against hand-computed values
assert approx(crypto.prob_itm(100,100,0.50,1.0,0.03), 0.4247), "ATM"
assert approx(crypto.prob_itm(95000,150000,0.625,1.0,0.03), 0.1598), "BTC 95k->150k = 16% (variance drag)"
assert crypto.prob_itm(100,50,0.15,1.0,0.03) > 0.99, "low-vol deep ITM ~ certain"
assert crypto.prob_itm(100,200,0.50,1.0,0.03) < 0.20, "deep OTM"
# complement
pa,pb = crypto.prob_itm(100,120,0.6,0.5), crypto.prob_below(100,120,0.6,0.5)
assert approx(pa+pb, 1.0, 1e-9), "complement"
# horizon monotonic
assert crypto.prob_itm(100,130,0.5,1.0) > crypto.prob_itm(100,130,0.5,1/12), "horizon"

# 2. parsing
assert crypto.parse_price_market("Will Bitcoin reach $150,000 by Dec 2026?") == ("BTC",150000.0,"above")
assert crypto.parse_price_market("Will BTC hit $200k in 2026?") == ("BTC",200000.0,"above")
assert crypto.parse_price_market("Ethereum above $10,000?") == ("ETH",10000.0,"above")
assert crypto.parse_price_market("Will Bitcoin dip below $80,000?") == ("BTC",80000.0,"below")
assert crypto.parse_price_market("Will the Fed cut rates?") is None

# 3. end-to-end with mock fetch
now=datetime.now(timezone.utc); exp=f"{now.year+1}-{now.month:02d}-{min(now.day,28):02d}"
def mock(url):
    if "get_index_price" in url: return {"result":{"index_price":95000.0}}
    if "get_instruments" in url:
        d=datetime.fromisoformat(exp); lbl=d.strftime("%d%b%y").upper()
        return {"result":[{"instrument_name":f"BTC-{lbl}-{k}-C"} for k in [80000,100000,120000,150000,200000]]}
    if "ticker" in url:
        import re; m=re.search(r"-(\d+)-C",url); k=int(m.group(1)) if m else 120000
        return {"result":{"mark_iv":55+abs(k-100000)/100000*15}}
    return {"result":{}}
r=crypto.implied_probability("BTC",150000,exp,"above",fetch=mock)
assert r["ok"] and 10 < r["prob_pct"] < 22, r  # ~16% at these inputs
# errors
assert not crypto.implied_probability("BTC",1,"bad-date")["ok"]
print("crypto engine: ALL TESTS PASS (%d checks)" % 14)
