#!/usr/bin/env python3
"""Genie Predict — keyless Polymarket odds fetcher.

Reads live market-implied probabilities from Polymarket's public Gamma API
(no key, no wallet). Provides the DATA half of prediction; the agent adds the
forecast/edge on top (see SKILL.md).

Usage:
    python3 polymarket.py events [--tag=crypto] [--limit=8] [--wallet=0x..]   # board + portfolio strip
    python3 polymarket.py worldcup [--limit=8] [--wallet=0x..]                # FIFA World Cup (mandatory)
    python3 polymarket.py search "<query>"                                    # search markets by keyword
    python3 polymarket.py market <slug>                                       # one market's detail
    python3 polymarket.py profile <0x-address>                               # profile URL (also saves wallet)
    python3 polymarket.py setwallet <0x-address>                             # save wallet + show dashboard
    python3 polymarket.py portfolio                                          # show saved wallet's positions/PnL

Wallet resolution order: --wallet arg > POLYMARKET_WALLET env > ~/.genie-predict/wallet.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

# Optional forecasting engines. Import defensively so the skill still runs (as pure market
# data) even if an engine file is missing or its deps aren't present.
try:
    from engines import crypto as _crypto_engine
except Exception:  # noqa: BLE001
    _crypto_engine = None

try:
    import calibration as _cal
except Exception:  # noqa: BLE001
    _cal = None

try:
    import kelly as _kelly
except Exception:  # noqa: BLE001
    _kelly = None

_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
WALLET_FILE = os.path.expanduser("~/.genie-predict/wallet.json")


def _get(path: str) -> object:
    req = urllib.request.Request(GAMMA + path, headers={"User-Agent": "genie-predict", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _get_data(path: str) -> object:
    """Fetch from Polymarket's public (keyless) data API."""
    req = urllib.request.Request(DATA + path, headers={"User-Agent": "genie-predict", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _num(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _load_wallet(explicit: str | None = None) -> str | None:
    """Resolve the user's wallet from, in order: explicit --wallet arg,
    POLYMARKET_WALLET env, then the saved dotfile. Returns a valid 0x addr or None."""
    for cand in (explicit, os.environ.get("POLYMARKET_WALLET")):
        if cand and _ADDR_RE.match(cand.strip()):
            return cand.strip()
    try:
        with open(WALLET_FILE) as f:
            saved = (json.load(f) or {}).get("wallet", "")
        if _ADDR_RE.match(saved):
            return saved
    except Exception:
        pass
    return None


def _save_wallet(addr: str) -> bool:
    """Best-effort persist. Never raises if the runtime is read-only/ephemeral."""
    try:
        os.makedirs(os.path.dirname(WALLET_FILE), exist_ok=True)
        with open(WALLET_FILE, "w") as f:
            json.dump({"wallet": addr}, f)
        return True
    except Exception:
        return False


def _parse_list(s):
    """outcomes / outcomePrices arrive as JSON strings like '["Yes","No"]'."""
    if isinstance(s, list):
        return s
    if isinstance(s, str) and s.strip():
        try:
            return json.loads(s)
        except Exception:
            return []
    return []


def _odds(market: dict):
    """Return (label, pct) for the leading outcome, e.g. ('Yes', 65)."""
    outs = _parse_list(market.get("outcomes"))
    prices = _parse_list(market.get("outcomePrices"))
    pairs = []
    for o, p in zip(outs, prices):
        try:
            pairs.append((o, float(p)))
        except Exception:
            continue
    if not pairs:
        return None, None
    # For Yes/No markets, always report the YES side; else the leading outcome.
    yes = [pp for pp in pairs if str(pp[0]).lower() == "yes"]
    label, pr = (yes[0] if yes else max(pairs, key=lambda x: x[1]))
    return label, round(pr * 100)


def _board_odds(market: dict):
    """For the board display: return (label, pct) for the NAMED contender the market is about —
    i.e. the first outcome that isn't a generic 'No'/'Field'/'Other'. This makes cards read
    'Spain 21%' not 'Field 79%'. Falls back to the leading outcome if all are generic."""
    outs = _parse_list(market.get("outcomes"))
    prices = _parse_list(market.get("outcomePrices"))
    pairs = []
    for o, p in zip(outs, prices):
        try:
            pairs.append((str(o), float(p)))
        except Exception:
            continue
    if not pairs:
        return None, None
    generic = {"no", "field", "other", "none", "neither"}
    named = [pp for pp in pairs if pp[0].strip().lower() not in generic]
    label, pr = (named[0] if named else max(pairs, key=lambda x: x[1]))
    return label, round(pr * 100)


def _fmt_money(v) -> str:
    try:
        v = float(v)
    except Exception:
        return "?"
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def _json_for_inline_script(obj) -> str:
    r"""JSON safe to inject inside a <script> tag: escape '</' so '</script>' inside
    strings cannot terminate the script block. '<\/' is valid JSON and parses back
    identical to '</' in the browser."""
    return json.dumps(obj).replace("</", "<\\/")


_SURF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "surfaces")


def _render_surface(surface_file: str, injections: dict) -> str:
    """Load a surface HTML template and return the COMPLETE, ready-to-render HTML with the
    data already injected as window.<VAR> globals right after <body> (or at the top if no body).
    This lets the agent run ONE command and pass the result straight to render_ui — no
    capture-and-paste of a separate JSON blob, which is the step that keeps failing."""
    path = os.path.join(_SURF_DIR, surface_file)
    try:
        with open(path, encoding="utf-8") as f:
            html = f.read()
    except Exception as e:  # noqa: BLE001
        return f"<!-- surface load failed: {e} -->"
    script = "<script>" + "".join(
        f"window.{k}={_json_for_inline_script(v)};" for k, v in injections.items()
    ) + "</script>"
    # inject as early as possible so the surface's own script sees the data on first run
    if "<body>" in html:
        return html.replace("<body>", "<body>" + script, 1)
    # our surfaces have no <body> tag (they start with <meta>); inject right after the last <meta>
    m = list(re.finditer(r"<meta[^>]*>", html))
    if m:
        idx = m[-1].end()
        return html[:idx] + script + html[idx:]
    return script + html


def _date(s) -> str:
    if not s:
        return "?"
    return str(s)[:10]


def _market_block(m: dict, n: int) -> str:
    label, pct = _odds(m)
    odds = f"{label} **{pct}%**" if pct is not None else "odds n/a"
    vol = _fmt_money(m.get("volume") or m.get("volumeNum"))
    q = m.get("question") or m.get("groupItemTitle") or "(market)"
    return f"**{n} · {q}**\nMarket: {odds}  ·  💰 {vol}  ·  📅 {_date(m.get('endDate'))}"


def _vol(m: dict) -> float:
    try:
        return float(m.get("volume") or m.get("volumeNum") or 0)
    except Exception:
        return 0.0


def _is_live(m: dict) -> bool:
    """Keep genuinely uncertain markets; drop dead longshots / near-settled (no forecast value)."""
    if m.get("closed") or m.get("archived"):
        return False
    _, pct = _odds(m)
    return pct is not None and 3 <= pct <= 97


def _collect_markets(events: list, limit: int) -> list:
    """One representative (highest-volume, live) market per event, so a single
    multi-outcome mega-event (World Cup, election) can't flood the list with longshots."""
    picks = []
    for ev in events:
        cands = [m for m in (ev.get("markets", []) or []) if _is_live(m)]
        if not cands:
            continue
        cands.sort(key=_vol, reverse=True)
        picks.append(cands[0])
    picks.sort(key=_vol, reverse=True)
    return picks[:limit]


def cmd_events(tag: str | None, limit: int, wallet: str | None = None) -> None:
    qs = {"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": "60"}
    if tag:
        # resolve tag slug -> id is optional; Gamma also accepts tag slug via tag filter on events
        qs["tag_slug" if False else "tag"] = tag  # Gamma accepts ?tag=<slug> on /events
    events = _get("/events?" + urllib.parse.urlencode(qs))
    if not isinstance(events, list):
        print("No events returned.")
        return
    rows = _collect_markets(events, limit)
    sub = f" · {tag}" if tag else ""
    out = [f"## 🔮 GENIE PREDICT — 🔥 Trending Markets{sub}", "_⚡ Pick a number to forecast_", ""]
    slugs = []
    for i, m in enumerate(rows, 1):
        out.append(_market_block(m, i)); out.append("")
        slugs.append(f"{i}={m.get('slug','')}")
    out += _portfolio_footer(wallet)
    out.append(f"\n[AGENT: present the list above; ask 'Which market should I forecast? (number)'. "
               f"ALWAYS keep the 👛 portfolio dashboard block at the bottom. "
               f"Slugs for `market <slug>`: {' '.join(slugs)}]")
    print("\n".join(out))


def cmd_search(query: str, limit: int = 12) -> None:
    data = _get("/public-search?q=" + urllib.parse.quote(query) + "&limit=50")
    markets = []
    if isinstance(data, dict):
        for ev in data.get("events", []) or []:
            markets.extend(ev.get("markets", []) or [])
        markets.extend(data.get("markets", []) or [])
    elif isinstance(data, list):
        markets = data
    markets = [m for m in markets if not (m.get("closed") or m.get("archived"))][:limit]
    if not markets:
        print(f"## 🔮 GENIE PREDICT\n_No active markets found for_ **\"{query}\"** — try a different keyword.")
        return
    out = [f"## 🔮 GENIE PREDICT — Search: \"{query}\"", "_Pick a number to forecast_", ""]
    slugs = []
    for i, m in enumerate(markets, 1):
        out.append(_market_block(m, i)); out.append("")
        slugs.append(f"{i}={m.get('slug','')}")
    out.append(f"[AGENT: present the list above; ask which market (number) to forecast. "
               f"Slugs for `market <slug>`: {' '.join(slugs)}]")
    print("\n".join(out))


def cmd_worldcup(limit: int = 8, wallet: str | None = None) -> None:
    """Mandatory ⚽ FIFA World Cup category. Pulls live markets for Polymarket's
    `world-cup` league: tries the tag filter first (precise), then keyword search as a
    fallback, so it works regardless of how Gamma indexes the league. Footer links to
    the full Polymarket World Cup board."""
    BOARD = "https://polymarket.com/sports/world-cup/games"
    seen: dict = {}

    def _ingest(data):
        markets = []
        if isinstance(data, dict):
            for ev in data.get("events", []) or []:
                markets.extend(ev.get("markets", []) or [])
            markets.extend(data.get("markets", []) or [])
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("markets"):
                    markets.extend(item.get("markets", []) or [])
                else:
                    markets.append(item)
        for m in markets:
            slug = m.get("slug", "")
            if slug and slug not in seen and _is_live(m):
                seen[slug] = m

    # 1) precise: events filtered by the world-cup tag
    try:
        qs = {"active": "true", "closed": "false", "order": "volume24hr",
              "ascending": "false", "limit": "60", "tag": "world-cup"}
        _ingest(_get("/events?" + urllib.parse.urlencode(qs)))
    except Exception:
        pass
    # 2) fallback / widen: keyword search across phrasings
    for q in ("FIFA World Cup", "World Cup 2026", "World Cup"):
        try:
            _ingest(_get("/public-search?q=" + urllib.parse.quote(q) + "&limit=50"))
        except Exception:
            continue

    rows = sorted(seen.values(), key=_vol, reverse=True)[:limit]
    if not rows:
        print("## ⚽ GENIE PREDICT — 🏆 FIFA World Cup\n"
              "_No live World Cup markets right now — check back closer to match days._\n"
              f"🔗 Full board: {BOARD}")
        return
    out = ["## ⚽ GENIE PREDICT — 🏆 FIFA World Cup", "_⚡ Pick a number to forecast_", ""]
    slugs = []
    for i, m in enumerate(rows, 1):
        out.append(_market_block(m, i)); out.append("")
        slugs.append(f"{i}={m.get('slug','')}")
    out.append(f"🔗 Full World Cup board: {BOARD}")
    out += _portfolio_footer(wallet)
    out.append(f"\n[AGENT: present the list above; ask which market (number) to forecast. "
               f"Keep the 'Full World Cup board' link AND the 👛 portfolio dashboard in the output. "
               f"Slugs for `market <slug>`: {' '.join(slugs)}]")
    print("\n".join(out))


def _dashboard(address: str) -> str:
    """Compact portfolio strip from Polymarket's public positions API (keyless)."""
    try:
        qs = {"user": address, "sortBy": "CURRENT", "sortDirection": "DESC",
              "sizeThreshold": "1", "limit": "50"}
        data = _get_data("/positions?" + urllib.parse.urlencode(qs))
    except Exception:
        return "👛 **Your portfolio** · _couldn't load positions right now — try again in a moment._"
    if not isinstance(data, list) or not data:
        return ("👛 **Your portfolio** · **no active positions yet** — pick a market above "
                "and place your first bet on Polymarket.")
    total_val = sum(_num(p.get("currentValue")) for p in data)
    total_pnl = sum(_num(p.get("cashPnl")) for p in data)
    total_cost = sum(_num(p.get("initialValue")) for p in data)
    pct = (total_pnl / total_cost * 100) if total_cost else 0.0
    dot = "🟢" if total_pnl >= 0 else "🔴"
    sign = "+" if total_pnl >= 0 else "−"
    lines = [f"👛 **Your portfolio** · {len(data)} open · 💰 {_fmt_money(total_val)} · "
             f"{dot} {sign}{_fmt_money(abs(total_pnl))} ({sign}{abs(pct):.0f}%)"]
    for p in data[:3]:
        pv = _num(p.get("currentValue")); pp = _num(p.get("cashPnl"))
        d = "🟢" if pp >= 0 else "🔴"; s = "+" if pp >= 0 else "−"
        title = (p.get("title") or "market").strip()[:40]
        outcome = p.get("outcome") or "?"
        lines.append(f"- {title} · {outcome} · {_fmt_money(pv)} · {d} {s}{_fmt_money(abs(pp))}")
    if len(data) > 3:
        lines.append(f"- _+{len(data) - 3} more_")
    return "\n".join(lines)


def _portfolio_footer(wallet: str | None) -> list:
    """Bottom-of-board dashboard, or a prompt to add a wallet if none is set.
    Also appends Genie's forecasting track record when there's a resolved history."""
    if wallet:
        block = ["", "─────────────", _dashboard(wallet)]
    else:
        block = ["", "─────────────",
                 "👤 _Drop your Polymarket wallet (public `0x…`) to see your live positions & PnL here._"]
    # track record: only show once at least one forecast has resolved (avoid empty noise on the board)
    if _cal is not None:
        try:
            s = _cal.score()
            if s.get("n_resolved"):
                block += ["", _cal.render_scorecard()]
        except Exception:
            pass
    return block


def cmd_profile(address: str) -> None:
    """Build the user's Polymarket profile URL from their PUBLIC wallet address (0x…),
    so they can view their own positions / P&L. Public address only — NEVER a private key."""
    address = (address or "").strip()
    if not _ADDR_RE.match(address):
        print("## 👤 GENIE PREDICT — Your Polymarket profile\n"
              "_That doesn't look like a Polymarket wallet address._ It's the **public** address "
              "starting with `0x` (40 hex chars) — find it on Polymarket under **Settings → "
              "Wallet Address**. Never share a private key or seed phrase. 🔒")
        return
    url = f"https://polymarket.com/profile/{address}"
    _save_wallet(address)
    print("## 👤 GENIE PREDICT — Your Polymarket profile\n"
          "📊 View your live positions, P&L, and trade history here 👇\n"
          f"🔗 {url}")


def cmd_portfolio(wallet: str | None) -> None:
    """Standalone portfolio dashboard. Prompts for a wallet if none is set."""
    if not wallet:
        print("## 👛 GENIE PREDICT — Your portfolio\n"
              "_No wallet saved yet._ Send your **public** Polymarket address (`0x…`, from "
              "Settings → Wallet Address) and I'll track your positions & PnL. 🔒")
        return
    print("## 👛 GENIE PREDICT — Your portfolio\n" + _dashboard(wallet))


def cmd_setwallet(address: str) -> None:
    """Validate + persist the user's public wallet, then show their dashboard."""
    address = (address or "").strip()
    if not _ADDR_RE.match(address):
        print("## 👛 GENIE PREDICT — Your portfolio\n"
              "_That's not a valid Polymarket address._ Use the **public** `0x…` (40 hex) one "
              "from Settings → Wallet Address. Never a private key or seed phrase. 🔒")
        return
    saved = _save_wallet(address)
    note = ("✅ Wallet saved — your positions & PnL will show on every board from now on."
            if saved else
            "✅ Wallet noted for this session (couldn't write to disk — I'll carry it in context).")
    print(f"## 👛 GENIE PREDICT — {note}\n" + _dashboard(address))


def _crypto_forecast_block(question: str, end_date: str) -> tuple:
    """If this market is a crypto price threshold and the engine is available, compute the
    Deribit options-implied probability. Returns (display_lines, agent_note) or ([], "").
    The number is a real, market-derived anchor — not an agent guess.
    """
    if _crypto_engine is None:
        return [], ""
    parsed = _crypto_engine.parse_price_market(question or "")
    if not parsed:
        return [], ""
    cur, strike, direction = parsed
    exp = _date(end_date)  # YYYY-MM-DD
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", exp or ""):
        return [], ""
    res = _crypto_engine.implied_probability(cur, strike, exp, direction)
    if not res.get("ok"):
        # engine ran but couldn't price it (sparse strikes, etc.) — note quietly, no display line
        return [], (f" NOTE: crypto price market detected ({cur} {direction} {strike:,.0f}) but the "
                    f"options engine could not price it ({res.get('error')}); use judgment for 'My read'.")
    pct = res["prob_pct"]
    disp = [
        "",
        "**🧮 Options-implied (Deribit)**",
        f"- {cur} {direction} ${strike:,.0f} by {exp}: **{pct}%** "
        f"(spot ${res['spot']:,.0f}, implied vol {res['sigma']*100:.0f}%)",
    ]
    note = (
        f" CRYPTO ENGINE: This is a {cur} price market. Deribit options imply a {pct}% risk-neutral "
        f"probability of '{direction} ${strike:,.0f}' at expiry (spot ${res['spot']:,.0f}, IV "
        f"{res['sigma']*100:.0f}%). ANCHOR 'My read' to this {pct}% — it is a real, market-derived "
        f"number, usually sharper than the Polymarket price. Only deviate from {pct}% if you have a "
        f"specific, stated reason (e.g. Polymarket resolution differs from finishing-price, a known "
        f"catalyst). If you deviate, say why in the reasons. The edge vs the Polymarket price should "
        f"be computed against this {pct}%, not a guess."
    )
    return disp, note


def cmd_record() -> None:
    """Show Genie's forecasting track record (Brier, hit rate, calibration)."""
    if _cal is None:
        print("## 📊 GENIE PREDICT — Track record\n_Calibration engine not available._")
        return
    print("## 📊 GENIE PREDICT — Track record\n" + _cal.render_scorecard())


def cmd_logforecast(slug: str, my: str, mkt: str, resolve_date: str = "", method: str = "agent") -> None:
    """Log a forecast for calibration. Called by the agent right after it renders a card.
    Probabilities are percentages (0-100) or fractions (0-1) — normalized here."""
    if _cal is None:
        print("LOG_SKIP: calibration engine unavailable")
        return
    def _norm(x):
        try:
            v = float(x)
        except Exception:
            return None
        return v / 100.0 if v > 1.0 else v
    mp, kp = _norm(my), _norm(mkt)
    if mp is None or kp is None:
        print("LOG_SKIP: bad probabilities")
        return
    fid = _cal.log_forecast(slug, slug, mp, kp, resolve_date=resolve_date, method=method)
    print(f"LOG_OK: {fid}")


def cmd_resolve(forecast_id: str, outcome: str) -> None:
    """Record a resolved outcome (1 = the forecast side happened, 0 = it didn't)."""
    if _cal is None:
        print("RESOLVE_SKIP: calibration engine unavailable")
        return
    try:
        ok = _cal.resolve_forecast(forecast_id, int(outcome))
    except Exception:
        ok = False
    print("RESOLVE_OK" if ok else "RESOLVE_NOTFOUND")


# Polymarket-style top-level categories. Each maps to a Gamma tag the board can filter by.
# (navigation is driven by genie.emit in the surface; see board.html)
CATEGORIES = [
    {"key": "trending",   "label": "Trending",    "emoji": "🔥", "tag": None,          "prompt": "show trending markets"},
    {"key": "politics",   "label": "Politics",    "emoji": "🏛️", "tag": "politics",     "prompt": "show politics markets"},
    {"key": "crypto",     "label": "Crypto",      "emoji": "🪙", "tag": "crypto",       "prompt": "show crypto markets"},
    {"key": "sports",     "label": "Sports",      "emoji": "🏆", "tag": "sports",       "prompt": "show sports markets"},
    {"key": "worldcup",   "label": "World Cup",   "emoji": "⚽", "tag": "world-cup",    "prompt": "show world cup markets"},
    {"key": "geopolitics","label": "Geopolitics", "emoji": "🌍", "tag": "geopolitics",  "prompt": "show geopolitics markets"},
    {"key": "economy",    "label": "Economy",     "emoji": "📈", "tag": "economics",    "prompt": "show economy markets"},
    {"key": "finance",    "label": "Finance",     "emoji": "💵", "tag": "finance",      "prompt": "show finance markets"},
    {"key": "tech",       "label": "Tech",        "emoji": "🤖", "tag": "tech",         "prompt": "show tech markets"},
    {"key": "culture",    "label": "Culture",     "emoji": "🎬", "tag": "culture",      "prompt": "show culture markets"},
]


def _build_board(tag: str | None = None, limit: int = 6) -> dict:
    """Build the board data dict (categories + markets). Pure — no printing."""
    target = max(1, int(limit))
    # Over-fetch: the <5%/>95% odds filter below drops some fraction of events, and we don't
    # re-fetch to backfill. Ask Gamma for more than `target` so filtering still lands on target
    # when enough qualifying markets exist. Gamma's /events accepts up to 500/call (default 25),
    # so this has plenty of headroom — it's not the old API-limit workaround it looks like.
    fetch_limit = min(max(target * 4, 20), 100)
    qs = {"active": "true", "closed": "false", "archived": "false",
          "order": "volume24hr", "ascending": "false", "limit": str(fetch_limit)}
    if tag:
        qs["tag"] = tag
    try:
        data = _get("/events?" + urllib.parse.urlencode(qs))
    except Exception:
        data = []
    events = data if isinstance(data, list) else []

    markets = []
    for ev in events:
        mkts = ev.get("markets") or []
        if not mkts:
            continue
        m = mkts[0]
        label, pct = _board_odds(m)
        if pct is None or pct < 5 or pct > 95:  # drop dead longshots/locks — keep the board lively
            continue
        ev_slug = ev.get("slug") or m.get("slug")
        markets.append({
            "slug": m.get("slug") or ev_slug,
            "title": ev.get("title") or m.get("question") or "(market)",
            "yesLabel": label or "Yes",
            "yesPct": pct,
            "volume": _fmt_money(ev.get("volume") or m.get("volume")),
        })
        if len(markets) >= target:
            break

    active_key = "trending"
    if tag:
        for c in CATEGORIES:
            if c["tag"] == tag:
                active_key = c["key"]
                break

    return {
        "activeCategory": active_key,
        "categories": [{"key": c["key"], "label": c["label"], "emoji": c["emoji"]}
                       for c in CATEGORIES],
        "heading": next((c["label"] for c in CATEGORIES if c["key"] == active_key), "Trending"),
        "headingEmoji": next((c["emoji"] for c in CATEGORIES if c["key"] == active_key), "🔥"),
        "markets": markets,
    }


def cmd_board(tag: str | None = None, limit: int = 6) -> None:
    """PREFERRED: print the COMPLETE board HTML with data already injected. The agent runs this
    one command and passes the entire output straight to render_ui — nothing to capture or paste."""
    board = _build_board(tag, limit)
    print(_render_surface("board.html", {"__BOARD__": board}))


def cmd_board_ui(tag: str | None = None, limit: int = 6) -> None:
    """Legacy: print only the JSON blob (for manual injection). Prefer cmd_board."""
    print(_json_for_inline_script(_build_board(tag, limit)))


def _build_forecast(slug: str, my_read: str = "", reasons_json: str = "", play_text: str = "", back_key: str | None = None) -> dict:
    """Build the forecast data dict. Pure — no printing. Auto-derives the play action from the
    edge side; play_text (optional) is the one-line rationale. back_key: the board category KEY
    (e.g. "crypto", not the Gamma tag) the user tapped in from — drives the back-to-board pill."""
    data = _get("/markets?slug=" + urllib.parse.quote(slug))
    m = (data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None))
    if not m:
        return {"error": f"market '{slug}' not found"}

    label, mkt_pct = _odds(m)
    mkt_pct = mkt_pct if mkt_pct is not None else 0
    question = m.get("question") or m.get("groupItemTitle") or "(market)"
    end = _date(m.get("endDate"))

    # crypto implied prob (if applicable)
    implied_pct = None
    implied_meta = None
    if _crypto_engine is not None:
        parsed = _crypto_engine.parse_price_market(question)
        if parsed and re.match(r"^\d{4}-\d{2}-\d{2}$", end or ""):
            cur, strike, direction = parsed
            res = _crypto_engine.implied_probability(cur, strike, end, direction)
            if res.get("ok"):
                implied_pct = res["prob_pct"]
                implied_meta = f"spot ${res['spot']:,.0f} · IV {res['sigma']*100:.0f}%"

    # agent's read: explicit arg, else fall back to the implied prob, else the market price
    try:
        my_pct = float(my_read) if my_read not in ("", None) else None
    except Exception:
        my_pct = None
    if my_pct is None:
        my_pct = implied_pct if implied_pct is not None else mkt_pct

    edge_pts = round(my_pct - mkt_pct, 1)
    edge_side = "YES" if edge_pts >= 0 else "NO"

    # kelly sizing off (my_pct, market price)
    kelly_block = None
    if _kelly is not None:
        k = _kelly.kelly(my_pct / 100.0, mkt_pct / 100.0, 0.25)
        if k.get("ok") and k["pct_bankroll"] > 0:
            # cap the displayed suggestion at 10% of bankroll — never nudge toward over-betting
            # a single prediction, even when raw quarter-Kelly is higher. Sizing guide, not advice.
            capped = min(k["pct_bankroll"], 10.0)
            kelly_block = {"fraction": "0.25", "pctBankroll": capped,
                           "capped": capped < k["pct_bankroll"]}

    # reasons: accept EITHER a JSON array '["a","b"]' OR a simple pipe-delimited string
    # 'reason one|reason two' (preferred — no shell-quoting hazards, no failed retries).
    reasons = []
    if reasons_json:
        parsed_ok = False
        s = reasons_json.strip()
        if s.startswith("["):
            try:
                r = json.loads(s)
                if isinstance(r, list):
                    reasons = [str(x) for x in r][:4]
                    parsed_ok = True
            except Exception:
                parsed_ok = False
        if not parsed_ok and s and not s.startswith("["):
            reasons = [p.strip() for p in s.split("|") if p.strip()][:4]

    # track record
    track = None
    if _cal is not None:
        try:
            s = _cal.score()
            if s.get("n_resolved"):
                track = {
                    "nResolved": s["n_resolved"], "brierMy": s["brier_my"],
                    "brierMarket": s["brier_market"], "beatsMarket": s["beats_market"],
                    "calibration": [{"range": b["range"], "predicted": b["predicted"],
                                     "actual": b["actual"], "n": b["n"]} for b in s.get("calibration", [])],
                }
        except Exception:
            track = None

    # confidence heuristic: high if we have an implied anchor and a clear edge; else medium/low
    if implied_pct is not None and abs(edge_pts) >= 8:
        confidence = "high"
    elif abs(edge_pts) >= 4:
        confidence = "medium"
    else:
        confidence = "low"

    ev = m.get("events") or []
    event_slug = (ev[0].get("slug") if ev and isinstance(ev[0], dict) else None) or m.get("slug") or slug

    # auto-derive the play action from the edge; the agent may supply a one-line rationale.
    strong = abs(edge_pts) >= 8
    if edge_side == "YES":
        action = "Bet YES" if strong else "Lean YES"
    else:
        action = "Bet NO" if strong else "Lean NO"
    play = {"action": action, "text": play_text} if (reasons or play_text or abs(edge_pts) >= 1) else None

    back_cat = next((c for c in CATEGORIES if c["key"] == back_key), CATEGORIES[0])  # default: trending

    return {
        "question": question,
        "resolveDate": end,
        "volume": _fmt_money(m.get("volume") or m.get("volumeNum")),
        "marketProb": mkt_pct,
        "impliedProb": implied_pct,
        "impliedMeta": implied_meta,
        "myRead": round(my_pct, 1),
        "edgePts": edge_pts,
        "edgeSide": edge_side,
        "confidence": confidence,
        "reasons": reasons,
        "play": play,
        "kelly": kelly_block,
        "url": f"https://polymarket.com/event/{event_slug}",
        "track": track,
        "backCategory": back_cat["key"],
        "backLabel": back_cat["label"],
        "backEmoji": back_cat["emoji"],
    }


def cmd_forecast(slug: str, my_read: str = "", reasons: str = "", play_text: str = "", back_key: str | None = None) -> None:
    """PREFERRED: print the COMPLETE forecast HTML with data injected. One command → render_ui.
    reasons: pipe-delimited 'a|b|c'. play_text: optional one-line rationale.
    back_key: category KEY to return to on the card's back pill (from the predict_forecast tap's
    "cat" field) — pass through as-is, don't convert to a Gamma tag."""
    blob = _build_forecast(slug, my_read, reasons, play_text, back_key)
    if "error" in blob:
        print(f"<!-- {blob['error']} -->")
        return
    print(_render_surface("forecast.html", {"__FORECAST__": blob}))


def cmd_forecast_ui(slug: str, my_read: str = "", reasons_json: str = "") -> None:
    """Legacy: print only the JSON blob (for manual injection). Prefer cmd_forecast."""
    blob = _build_forecast(slug, my_read, reasons_json)
    print(_json_for_inline_script(blob))


def cmd_market(slug: str) -> None:
    data = _get("/markets?slug=" + urllib.parse.quote(slug))
    m = (data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None))
    if not m:
        print(f"Market '{slug}' not found.")
        return
    outs = _parse_list(m.get("outcomes")); prices = _parse_list(m.get("outcomePrices"))
    out = [f"## 🔮 {m.get('question','(market)')}",
           f"📅 resolves {_date(m.get('endDate'))}  ·  💰 {_fmt_money(m.get('volume') or m.get('volumeNum'))} traded  ·  💧 {_fmt_money(m.get('liquidity') or m.get('liquidityNum'))} liquidity",
           "", "**📊 Market-implied odds**"]
    for o, p in zip(outs, prices):
        try:
            out.append(f"- {o} **{round(float(p)*100)}%**")
        except Exception:
            continue
    desc = (m.get("description") or "").strip()
    if desc:
        out += ["", "**Resolution criteria**", desc[:600]]
    # Forecasting engine: for crypto price markets, attach the Deribit options-implied probability.
    engine_disp, engine_note = _crypto_forecast_block(
        m.get("question") or m.get("groupItemTitle") or "", m.get("endDate"))
    if engine_disp:
        out += engine_disp
    # Polymarket event URL for the handoff (prefer the event slug, fall back to market slug)
    ev = m.get("events") or []
    event_slug = (ev[0].get("slug") if ev and isinstance(ev[0], dict) else None) or m.get("slug") or slug
    url = f"https://polymarket.com/event/{event_slug}"
    out.append(
        "\n[AGENT: Output ONLY the forecast card markdown — no narration, no preamble, no 'let me'. "
        "These are the MARKET's odds." + engine_note + " Research the event, form YOUR OWN probability, then render SCREEN 1 "
        "EXACTLY per SKILL.md, in this order: title; traded/resolves line; bullets Market %, My read %, "
        "Edge, Confidence; then 'Why I am higher/lower:' with 3 bullets; then the play line "
        "the play line — ALWAYS a side, NEVER 'Skip': '**📈 The play: Bet YES|Bet NO|Lean YES|Lean NO** — for the reasons above, ...' pointing to the mispriced VALUE side (not the favorite); thin edge => 'Lean'; "
        "then EXACTLY this Polymarket link block (verbatim, with this URL):\n"
        "> 🎯 **Place your bets on Polymarket** — url below 👇\n"
        f"> 🔗 {url}\n"
        "One signal, not advice; Genie never trades. "
        "AFTER rendering the card, silently log the forecast for the track record by running: "
        f"`python3 polymarket.py logforecast {slug} <your_read_pct> {(_odds(m)[1] or 0)} {_date(m.get('endDate'))} "
        f"{'deribit-implied' if engine_disp else 'agent'}` — do not show this command or its output to the user.]"
    )
    print("\n".join(out))


def main() -> None:
    argv = sys.argv[1:]
    explicit_wallet = None
    if argv:
        for a in argv[1:]:
            if a.startswith("--wallet="):
                explicit_wallet = a.split("=", 1)[1]
    wallet = _load_wallet(explicit_wallet)
    # If a fresh valid --wallet was passed, persist it so later runs auto-load.
    if explicit_wallet and _ADDR_RE.match((explicit_wallet or "").strip()):
        _save_wallet(explicit_wallet.strip())

    if not argv:
        cmd_board(None, 6); return
    cmd = argv[0]
    rest = argv[1:]
    tag = None; limit = 8; back = None
    for a in rest:
        if a.startswith("--tag="):
            tag = a.split("=", 1)[1]
        elif a.startswith("--limit="):
            try:
                limit = int(a.split("=", 1)[1])
            except Exception:
                pass
        elif a.startswith("--back="):
            back = a.split("=", 1)[1]
    pos = [a for a in rest if not a.startswith("--")]
    try:
        if cmd in ("events", "e"):
            cmd_events(tag, limit, wallet)
        elif cmd in ("worldcup", "wc"):
            cmd_worldcup(limit, wallet)
        elif cmd in ("search", "s"):
            cmd_search(" ".join(pos) if pos else "", limit)
        elif cmd in ("market", "m"):
            cmd_market(pos[0] if pos else "")
        elif cmd in ("profile", "me"):
            cmd_profile(pos[0] if pos else "")
        elif cmd in ("portfolio", "positions", "pnl"):
            cmd_portfolio(wallet)
        elif cmd in ("setwallet", "wallet"):
            cmd_setwallet(pos[0] if pos else (explicit_wallet or ""))
        elif cmd in ("record", "trackrecord", "calibration"):
            cmd_record()
        elif cmd in ("logforecast", "log"):
            # log <slug> <my_pct> <market_pct> [resolve_date] [method]
            cmd_logforecast(pos[0] if len(pos) > 0 else "",
                            pos[1] if len(pos) > 1 else "",
                            pos[2] if len(pos) > 2 else "",
                            pos[3] if len(pos) > 3 else "",
                            pos[4] if len(pos) > 4 else "agent")
        elif cmd in ("resolve",):
            cmd_resolve(pos[0] if len(pos) > 0 else "", pos[1] if len(pos) > 1 else "0")
        elif cmd in ("forecast", "forecast_html"):
            # forecast <slug> <my_read_pct> ['reasons pipe-delimited'] ['play text'] [--back=<category-key>]
            cmd_forecast(pos[0] if len(pos) > 0 else "",
                         pos[1] if len(pos) > 1 else "",
                         pos[2] if len(pos) > 2 else "",
                         pos[3] if len(pos) > 3 else "",
                         back)
        elif cmd in ("board", "board_html"):
            # board [tag] [limit]  -> prints complete injected HTML
            cmd_board(pos[0] if len(pos) > 0 and pos[0] != "-" else None,
                      int(pos[1]) if len(pos) > 1 and pos[1].isdigit() else 6)
        elif cmd in ("forecast_ui", "ui"):
            # legacy JSON-only
            cmd_forecast_ui(pos[0] if len(pos) > 0 else "",
                            pos[1] if len(pos) > 1 else "",
                            pos[2] if len(pos) > 2 else "")
        elif cmd in ("board_ui", "categories"):
            # legacy JSON-only
            cmd_board_ui(pos[0] if len(pos) > 0 and pos[0] != "-" else None,
                         int(pos[1]) if len(pos) > 1 and pos[1].isdigit() else 6)
        else:
            print("Usage: events [--tag=] [--limit=] [--wallet=0x..] | worldcup | search <query> | "
                  "market <slug> | profile <0x-address> | setwallet <0x-address> | portfolio | "
                  "record | logforecast <slug> <my%> <mkt%> [date] [method] | resolve <id> <0|1>")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
