import argparse
import json
import os
import requests
import socket
import time
import math
from datetime import datetime

# Cover DNS resolution — requests(timeout=N) does NOT protect against
# a hanging DNS lookup.  This global default applies to every socket
# operation in the process.
socket.setdefaulttimeout(5)

# ── Coin names verified against Hyperliquid universe (POST {"type":"meta"}) ───
# Mainnet indices: BTC=0  ETH=1  SOL=5  DOGE=12  SUI=14  LINK=18  XRP=25  ZEC=214
# All 8 coins confirmed on mainnet with these exact names (plain uppercase, no suffix).
# Testnet note: XRP and LINK are NOT in the testnet universe — scans for those two
# symbols will raise a ValueError which scan_symbol catches per-symbol gracefully.
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "SUI", "LINK", "XRP", "ZEC"]

# Coins available on TESTNET  (6 of 8):  BTC ETH SOL DOGE SUI ZEC
# Coins mainnet-only           (2 of 8):  LINK XRP
# Switch HL_INFO_URL to mainnet to scan all 8.

SYMBOL_SL_FLOORS = {
    # Tightest floors first (most liquid)
    "BTC":  0.003,
    "ETH":  0.0015,
    "SOL":  0.003,
    "DOGE": 0.005,
    "SUI":  0.005,
    "LINK": 0.003,
    "XRP":  0.005,
    "ZEC":  0.005,
    # Additional coins (not in default watchlist but recognised if added)
    "AAVE": 0.003,
    "BNB":  0.002,
}
DEFAULT_SL_FLOOR = 0.003

# ── Hyperliquid info endpoint ─────────────────────────────────────────────────
# Set HL_USE_MAINNET=true (env var) to switch to production mainnet.
# On mainnet all 8 coins are available.  On testnet LINK/XRP are missing and
# ZEC exists in the universe but has ZERO trading activity (no candles, empty book).
_USE_MAINNET = os.environ.get("HL_USE_MAINNET", "false").lower() in ("1", "true", "yes")
HL_INFO_URL  = (
    "https://api.hyperliquid.xyz/info"
    if _USE_MAINNET else
    "https://api.hyperliquid-testnet.xyz/info"
)

# ── Debug logging ─────────────────────────────────────────────────────────────
# Set HL_DEBUG_API=true to print raw API request/response for HL_DEBUG_COIN.
# Use HL_DEBUG_COIN=ETH to debug a different symbol.  Default coin is BTC.
_DEBUG_API  = os.environ.get("HL_DEBUG_API", "false").lower() in ("1", "true", "yes")
_DEBUG_COIN = os.environ.get("HL_DEBUG_COIN", "BTC").upper()

def _debug_log(label, payload, response):
    """Print raw API request and first 400 chars of response for the debug coin."""
    print(f"\n[DEBUG-API] {label}")
    print(f"  REQUEST : {json.dumps(payload)}")
    resp_str = json.dumps(response) if not isinstance(response, str) else response
    print(f"  RESPONSE: {resp_str[:600]}{'…' if len(resp_str) > 600 else ''}")
    print()

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
WHITE = "\033[97m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ── Interval mapping: MEXC-style or HL-style → canonical HL interval ──────────
# Hyperliquid candleSnapshot accepted intervals:
# "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w"
_HL_INTERVALS = {
    "Min1":  "1m",  "Min5":  "5m",  "Min15": "15m", "Min30": "30m",
    "Min60": "1h",  "Hour4": "4h",  "Hour8": "8h",  "Day1":  "1d",
    # Pass-through if already in HL format
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "8h":  "8h",
    "12h": "12h", "1d": "1d", "3d": "3d", "1w":  "1w",
}
# Minutes per interval — used to compute startTime window from limit
_INTERVAL_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "8h": 480,
    "12h": 720, "1d": 1440, "3d": 4320, "1w": 10080,
}


def _hl_post(payload, max_retries=3, _debug_coin=None):
    """POST JSON payload to the Hyperliquid info endpoint with automatic retry.

    _debug_coin: if provided and HL_DEBUG_API=true, logs raw request + response.
    """
    body = json.dumps(payload).encode()
    for attempt in range(max_retries):
        try:
            r = requests.post(
                HL_INFO_URL,
                data=body,
                headers={"Content-Type": "application/json"},
                timeout=8,
            )
            r.raise_for_status()
            result = r.json()
            if _debug_api and _debug_coin:
                _debug_log(f"{_debug_coin} → {payload.get('type', '?')}", payload, result)
            return result
        except Exception as exc:
            if attempt < max_retries - 1:
                time.sleep(1.5 * (attempt + 1))
            else:
                raise ValueError(
                    f"HL info POST failed after {max_retries} retries: {exc}"
                ) from exc


# Resolved once at import time so scanner_server can read it
_debug_api = _DEBUG_API


def fetch_klines(symbol, interval, limit=100):
    """
    Fetch OHLCV candles from Hyperliquid candleSnapshot.

    Parameters
    ----------
    symbol   : bare coin name, e.g. "ETH"  (no _USDT suffix)
    interval : MEXC-style ("Min5", "Min60") or HL-style ("5m", "1h") — both accepted
    limit    : number of candles to return

    Returns
    -------
    List of dicts: {time, open, high, low, close, vol}  (oldest first, ascending)
    """
    hl_iv  = _HL_INTERVALS.get(interval, interval)
    mins   = _INTERVAL_MINUTES.get(hl_iv, 5)
    now_ms = int(time.time() * 1000)

    # Use 2× window — HL candleSnapshot does not guarantee dense bars for
    # low-activity coins.  The extra headroom costs nothing (API filters by
    # time) and avoids missing bars when trading is sporadic.
    start_ms = now_ms - int(limit * mins * 60 * 1000 * 2.0)

    is_debug = _DEBUG_API and symbol.upper() == _DEBUG_COIN
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin":      symbol,
            "interval":  hl_iv,
            "startTime": start_ms,
            "endTime":   now_ms,
        },
    }
    raw = _hl_post(payload, _debug_coin=symbol if is_debug else None)
    if not isinstance(raw, list):
        raise ValueError(f"candleSnapshot unexpected response for {symbol}: {raw}")

    # Always log debug info for the debug coin (even without full raw dump)
    if is_debug:
        print(f"[DEBUG] {symbol} candleSnapshot/{hl_iv}: {len(raw)} raw bars returned "
              f"(window={start_ms} → {now_ms}, "
              f"span={(now_ms - start_ms)/3_600_000:.1f}h)")
        if raw:
            first, last = raw[0], raw[-1]
            print(f"  first bar: t={first.get('t')}  o={first.get('o')}  c={first.get('c')}  v={first.get('v')}")
            print(f"  last  bar: t={last.get('t')}   o={last.get('o')}  c={last.get('c')}  v={last.get('v')}")

    candles = [
        {
            "time":  c.get("t", c.get("T", 0)),
            "open":  float(c.get("o", 0)),
            "high":  float(c.get("h", 0)),
            "low":   float(c.get("l", 0)),
            "close": float(c.get("c", 0)),
            "vol":   float(c.get("v", 0)),
        }
        for c in raw
    ]
    candles.sort(key=lambda x: x["time"])   # ensure ascending order
    result = candles[-limit:]                # trim to requested limit

    # Warn when a coin has too few candles for reliable indicator computation
    if len(result) < 9:
        net = "mainnet" if _USE_MAINNET else "testnet"
        print(f"[WARN] {symbol} {hl_iv}: only {len(result)} candles on {net} "
              f"(need ≥9 for KDJ).  "
              f"Possible causes: coin not trading, newly listed, or very thin market.")
    return result


# Cache for metaAndAssetCtxs — reused across all symbols within the same scan cycle
_meta_cache: dict = {"ts": 0.0, "data": None}
_META_TTL_S = 10     # seconds before re-fetching


def _fetch_meta():
    """Return [meta, assetCtxs] from metaAndAssetCtxs, cached for _META_TTL_S seconds."""
    now = time.time()
    if _meta_cache["data"] and now - _meta_cache["ts"] < _META_TTL_S:
        return _meta_cache["data"]
    raw = _hl_post({"type": "metaAndAssetCtxs"})
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError(f"metaAndAssetCtxs unexpected response: {raw}")
    _meta_cache["ts"]   = now
    _meta_cache["data"] = raw
    return raw


def fetch_ticker(symbol):
    """
    Fetch ticker data for a single coin via Hyperliquid metaAndAssetCtxs.

    Returns a dict with keys used by the rest of scanner.py:
      lastPrice, riseFallRate, fundingRate, highPrice, lowPrice, amount24, vol24

    HL assetCtx keys: funding, openInterest, prevDayPx, dayNtlVlm, premium,
                      oraclePx, markPx, midPx, impactPxs, dayBaseVlm
    All values arrive as strings — we explicitly cast each field to float.
    """
    meta, asset_ctxs = _fetch_meta()
    universe = meta.get("universe", [])

    idx = next(
        (i for i, a in enumerate(universe)
         if a.get("name", "").upper() == symbol.upper()),
        None,
    )
    if idx is None or idx >= len(asset_ctxs):
        net = "mainnet" if _USE_MAINNET else "testnet"
        raise ValueError(
            f"Coin {symbol!r} not found in Hyperliquid {net} universe "
            f"(universe has {len(universe)} coins).  "
            f"Verify the coin name or set HL_USE_MAINNET=true."
        )

    ctx      = asset_ctxs[idx]
    mark_px  = float(ctx.get("markPx")    or ctx.get("midPx") or 0)
    mid_px   = float(ctx.get("midPx")     or mark_px)
    prev_day = float(ctx.get("prevDayPx") or mark_px)
    funding  = float(ctx.get("funding")   or 0)
    ntl_vol  = float(ctx.get("dayNtlVlm") or 0)   # notional USDT volume
    base_vol = float(ctx.get("dayBaseVlm") or 0)  # volume in coin units
    oracle   = float(ctx.get("oraclePx")  or mark_px)

    # riseFallRate: fractional 24h change — same sign/semantics as MEXC field
    rise_fall = (mark_px - prev_day) / prev_day if prev_day else 0.0

    if _DEBUG_API and symbol.upper() == _DEBUG_COIN:
        print(f"[DEBUG] {symbol} metaAndAssetCtxs idx={idx}")
        print(f"  raw ctx  : {ctx}")
        print(f"  parsed   : markPx={mark_px}  prevDayPx={prev_day}  "
              f"funding={funding}  ntlVol={ntl_vol}  baseVol={base_vol}")
        print(f"  derived  : riseFallRate={rise_fall:.6f} ({rise_fall*100:.3f}%)  "
              f"highPrice={max(mark_px, oracle)}  lowPrice={min(mark_px, oracle)}")

    return {
        "lastPrice":    mark_px,
        "midPrice":     mid_px,
        "oraclePrice":  oracle,
        "riseFallRate": rise_fall,
        "fundingRate":  funding,
        # highPrice / lowPrice not in assetCtxs — approximate using mark/oracle spread
        "highPrice":    max(mark_px, oracle),
        "lowPrice":     min(mark_px, oracle),
        "amount24":     ntl_vol,
        "vol24":        base_vol,
        "_ctx":         ctx,
    }


def fetch_depth(symbol, limit=20):
    """
    Fetch order book from Hyperliquid l2Book.

    Returns a dict compatible with the rest of scanner.py:
      {"bids": [[price, size], ...], "asks": [[price, size], ...]}
    Bids are sorted descending (best bid first).
    Asks are sorted ascending  (best ask first).

    HL l2Book response format per level:
      {"px": "price_str", "sz": "size_str", "n": num_orders}
    Sizes are in base coin units (e.g. BTC amounts, not notional).
    """
    is_debug = _DEBUG_API and symbol.upper() == _DEBUG_COIN
    payload  = {"type": "l2Book", "coin": symbol}
    raw = _hl_post(payload, _debug_coin=symbol if is_debug else None)
    if not isinstance(raw, dict) or "levels" not in raw:
        raise ValueError(f"l2Book unexpected response for {symbol}: {raw}")

    levels   = raw["levels"]          # [[bid_levels], [ask_levels]]
    raw_bids = levels[0] if len(levels) > 0 else []
    raw_asks = levels[1] if len(levels) > 1 else []

    bids = [[float(b["px"]), float(b["sz"])] for b in raw_bids[:limit]]
    asks = [[float(a["px"]), float(a["sz"])] for a in raw_asks[:limit]]

    bids.sort(key=lambda x: -x[0])   # descending — best bid first
    asks.sort(key=lambda x:  x[0])   # ascending  — best ask first

    if is_debug or (not bids and not asks):
        bid_vol = sum(b[1] for b in bids)
        ask_vol = sum(a[1] for a in asks)
        tot     = bid_vol + ask_vol
        if not bids and not asks:
            net = "mainnet" if _USE_MAINNET else "testnet"
            print(f"[WARN] {symbol} l2Book: EMPTY order book on {net} — "
                  f"bid_pct and ask_pct will be None (coin may not be trading).")
        if is_debug:
            print(f"[DEBUG] {symbol} l2Book: {len(bids)} bid levels, {len(asks)} ask levels")
            if bids:
                print(f"  best bid: px={bids[0][0]}  sz={bids[0][1]}")
                print(f"  worst bid: px={bids[-1][0]}  sz={bids[-1][1]}")
            if asks:
                print(f"  best ask: px={asks[0][0]}  sz={asks[0][1]}")
                print(f"  worst ask: px={asks[-1][0]}  sz={asks[-1][1]}")
            if tot > 0:
                print(f"  total vol: {tot:.6f}  B%={bid_vol/tot*100:.1f}%  S%={ask_vol/tot*100:.1f}%")
            else:
                print(f"  total vol: 0 (empty book — B%/S% will be None)")

    return {"bids": bids, "asks": asks}


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema(values, n):
    if len(values) < n:
        return None
    k = 2.0 / (n + 1)
    result = sum(values[:n]) / n
    for v in values[n:]:
        result = v * k + result * (1 - k)
    return result


def kdj(candles, n=9, m1=3, m2=3):
    if len(candles) < n:
        return None, None, None
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]

    rsv_list = []
    for i in range(n - 1, len(candles)):
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        c = closes[i]
        if hh == ll:
            rsv = 50.0
        else:
            rsv = (c - ll) / (hh - ll) * 100
        rsv_list.append(rsv)

    k_val = 50.0
    d_val = 50.0
    for rsv in rsv_list:
        k_val = (2.0 / 3.0) * k_val + (1.0 / 3.0) * rsv
        d_val = (2.0 / 3.0) * d_val + (1.0 / 3.0) * k_val

    j_val = 3 * k_val - 2 * d_val
    return k_val, d_val, j_val


def calc_indicators(candles):
    closes = [c["close"] for c in candles]
    vols = [c["vol"] for c in candles]
    return {
        "ma5": sma(closes, 5),
        "ma10": sma(closes, 10),
        "ma30": sma(closes, 30),
        "ma60": sma(closes, 60),
        "ema20": ema(closes, 20),
        "vol_ma5": sma(vols, 5),
        "vol_ma10": sma(vols, 10),
        "kdj": kdj(candles),
        "closes": closes,
        "candles": candles,
    }


def _ind_summary(ind):
    """Slim serialisable snapshot of one timeframe's indicators."""
    k, d, j = ind["kdj"]
    return {
        "ma5":      ind["ma5"],
        "ma10":     ind["ma10"],
        "ma30":     ind["ma30"],
        "ma60":     ind["ma60"],
        "ema20":    ind.get("ema20"),
        "vol_ma5":  ind["vol_ma5"],
        "vol_ma10": ind["vol_ma10"],
        "k": k, "d": d, "j": j,
    }


def classify_trend(klines_5m, klines_1h, current_price):
    """Classify market trend using MA structure and KDJ momentum."""
    if len(klines_5m) < 30 or len(klines_1h) < 60:
        return "Choppy"

    closes_1h = [c["close"] for c in klines_1h]
    closes_5m = [c["close"] for c in klines_5m]

    ma10_1h = sma(closes_1h, 10)
    ma30_1h = sma(closes_1h, 30)
    ma60_1h = sma(closes_1h, 60)
    ma5_5m  = sma(closes_5m,  5)
    ma10_5m = sma(closes_5m, 10)
    ma30_5m = sma(closes_5m, 30)

    if not all([ma10_1h, ma30_1h, ma60_1h, ma5_5m, ma10_5m, ma30_5m]):
        return "Choppy"

    _, _, j_cur  = kdj(klines_1h)
    _, _, j_prev = kdj(klines_1h[:-1])
    j_rising  = (j_cur is not None and j_prev is not None and j_cur > j_prev)
    j_falling = (j_cur is not None and j_prev is not None and j_cur < j_prev)

    pct_from_ma60 = (current_price - ma60_1h) / ma60_1h * 100
    bull_1h   = ma10_1h > ma30_1h > ma60_1h
    bear_1h   = ma10_1h < ma30_1h < ma60_1h
    bull_5m   = ma5_5m  > ma10_5m  > ma30_5m
    bear_5m   = ma5_5m  < ma10_5m  < ma30_5m
    ma_spread = (max(ma10_1h, ma30_1h, ma60_1h) - min(ma10_1h, ma30_1h, ma60_1h)) / ma60_1h * 100

    if current_price > ma60_1h and bull_1h and bull_5m and j_rising and pct_from_ma60 > 0.5:
        return "Strong Bull"
    if current_price < ma60_1h and bear_1h and bear_5m and j_falling and pct_from_ma60 < -0.5:
        return "Strong Bear"
    if current_price > ma60_1h and bull_1h:
        return "Bullish"
    if current_price < ma60_1h and bear_1h:
        return "Bearish"
    if abs(pct_from_ma60) < 0.5 and ma_spread < 0.3:
        return "Neutral"
    return "Choppy"


def score_long(price, ind5m, ind1h, ticker, depth, j15=None, j5m_prev=None, symbol=""):
    # ── Pre-score gates (run before any point is awarded) ────────────────────────
    _, _, _j1h = ind1h["kdj"]
    _, _, _j5  = ind5m["kdj"]
    if _j1h is not None and _j1h > 40:
        print(f"BOUNCE LONG BLOCKED {symbol} — J1H {_j1h:.1f} above 40 threshold — no confluence")
        return 0, [f"[GATE] J1H CONFLUENCE FAILED — j1h {_j1h:.1f} above 40 — no multi-timeframe oversold confluence"]
    if j5m_prev is not None and _j5 is not None and _j5 <= j5m_prev:
        print(f"BOUNCE LONG BLOCKED {symbol} — J5M {_j5:.1f} not rising from prev {j5m_prev:.1f} — no directional confirmation")
        return 0, [f"[GATE] J5M DIRECTION FAILED — j5m {_j5:.1f} not rising from prev {j5m_prev:.1f}"]
    score = 0
    details = []

    change_pct = float(ticker.get("riseFallRate", 0)) * 100

    ma60_1h = ind1h["ma60"]
    if ma60_1h and price > ma60_1h and change_pct > -3:
        score += 1
        details.append(f"[+] Price > 1h MA60 ({ma60_1h:.4f}), 24h chg {change_pct:.2f}%")
    else:
        details.append(f"[-] Price vs 1h MA60 / 24h chg")

    ma5 = ind5m["ma5"]
    ma10 = ind5m["ma10"]
    ma30 = ind5m["ma30"]
    ema20 = ind5m["ema20"]
    if ma5 and ma10 and ma30 and ema20:
        stacked = ma5 > ma10 > ma30
        converging = (abs(ma5 - ma10) / price < 0.003 and abs(ma10 - ma30) / price < 0.003)
        above_ema = price > ema20
        if (stacked or converging) and above_ema:
            score += 1
            details.append(f"[+] 5m MAs bullish stacked/converging, price > EMA20")
        else:
            details.append(f"[-] 5m MA structure not bullish")
    else:
        details.append(f"[-] 5m MAs insufficient data")

    if ma10 and ma30 and ema20:
        cluster_vals = [ma10, ma30, ema20]
        closest = min(cluster_vals, key=lambda x: abs(x - price))
        if abs(price - closest) / price < 0.005:
            score += 1
            details.append(f"[+] Price within 0.5% of 5m MA cluster (pullback)")
        else:
            details.append(f"[-] Price not near 5m MA cluster")
    else:
        details.append(f"[-] 5m cluster data insufficient")

    k5, d5, j5 = ind5m["kdj"]
    if j5 is not None and j5 < 15:
        score += 1
        details.append(f"[+] 5m KDJ J={j5:.1f} < 15 (oversold)")
    else:
        j5_str = f"{j5:.1f}" if j5 is not None else "N/A"
        details.append(f"[-] 5m KDJ J={j5_str} not < 15")

    k1h, d1h, j1h = ind1h["kdj"]
    if j1h is not None and j1h < 50:
        score += 1
        details.append(f"[+] 1h KDJ J={j1h:.1f} < 50 (not overbought)")
    else:
        j1h_str = f"{j1h:.1f}" if j1h is not None else "N/A"
        details.append(f"[-] 1h KDJ J={j1h_str} not < 50")

    bids = depth.get("bids", [])
    asks = depth.get("asks", [])
    bid_vol = sum(float(b[1]) for b in bids) if bids else 0
    ask_vol = sum(float(a[1]) for a in asks) if asks else 0
    total_vol = bid_vol + ask_vol
    if total_vol > 0 and bid_vol / total_vol >= 0.6:
        score += 1
        details.append(f"[+] Buy depth {bid_vol/(total_vol)*100:.1f}% >= 60%")
    else:
        pct = (bid_vol / total_vol * 100) if total_vol > 0 else 0
        details.append(f"[-] Buy depth {pct:.1f}% < 60%")

    if bids and len(bids) >= 2:
        bid_sizes = [float(b[1]) for b in bids]
        avg_bid = sum(bid_sizes) / len(bid_sizes)
        max_bid = max(bid_sizes)
        max_bid_price = float(bids[bid_sizes.index(max_bid)][0])
        wall_close = abs(price - max_bid_price) / price < 0.002
        if avg_bid > 0 and max_bid >= 5 * avg_bid and wall_close:
            score += 1
            details.append(f"[+] Large bid wall {max_bid:.1f} >= 5x avg, within 0.2% of price")
        else:
            details.append(f"[-] No significant bid wall near price")
    else:
        details.append(f"[-] Insufficient bid data")

    candles5m = ind5m["candles"]
    vol_ma5 = ind5m["vol_ma5"]
    if vol_ma5 and len(candles5m) >= 10:
        recent10 = candles5m[-10:]
        max_vol_10 = max(c["vol"] for c in recent10)
        last3 = candles5m[-3:]
        green_count = sum(1 for c in last3 if c["close"] > c["open"])
        if max_vol_10 >= 2 * vol_ma5 and green_count >= 2:
            score += 1
            details.append(f"[+] Vol surge (max10={max_vol_10:.0f} >= 2x MA5), last 3 mostly green")
        else:
            details.append(f"[-] No vol surge with green candles")
    else:
        details.append(f"[-] Insufficient 5m volume data")

    vol_ma10 = ind5m["vol_ma10"]
    if len(candles5m) >= 1 and vol_ma10:
        last = candles5m[-1]
        if last["vol"] >= vol_ma10 and last["close"] > last["open"]:
            score += 1
            details.append(f"[+] Last candle vol >= MA10 and green")
        else:
            details.append(f"[-] Last candle vol/color not bullish")
    else:
        details.append(f"[-] Insufficient last candle data")

    funding = float(ticker.get("fundingRate", 0)) * 100
    if -0.01 <= funding <= 0.01:
        score += 1
        details.append(f"[+] Funding rate {funding:.4f}% in neutral range")
    else:
        details.append(f"[-] Funding rate {funding:.4f}% outside neutral range")

    if j15 is not None:
        if j15 < 30:
            score += 1
            details.append(f"[+] 15m KDJ J={j15:.1f} < 30 (momentum not overbought)")
        else:
            details.append(f"[-] 15m KDJ J={j15:.1f} not < 30")

    return score, details


def score_short(price, ind5m, ind1h, ticker, depth, j15=None, j5m_prev=None, symbol=""):
    # ── Pre-score gates (run before any point is awarded) ────────────────────────
    _, _, _j1h = ind1h["kdj"]
    _, _, _j5  = ind5m["kdj"]
    if _j1h is not None and _j1h < 60:
        print(f"BOUNCE SHORT BLOCKED {symbol} — J1H {_j1h:.1f} below 60 threshold — no confluence")
        return 0, [f"[GATE] J1H CONFLUENCE FAILED — j1h {_j1h:.1f} below 60 — no multi-timeframe overbought confluence"]
    if j5m_prev is not None and _j5 is not None and _j5 >= j5m_prev:
        print(f"BOUNCE SHORT BLOCKED {symbol} — J5M {_j5:.1f} not falling from prev {j5m_prev:.1f} — no directional confirmation")
        return 0, [f"[GATE] J5M DIRECTION FAILED — j5m {_j5:.1f} not falling from prev {j5m_prev:.1f}"]
    score = 0
    details = []

    change_pct = float(ticker.get("riseFallRate", 0)) * 100

    ma60_1h = ind1h["ma60"]
    if ma60_1h and price < ma60_1h and change_pct < 3:
        score += 1
        details.append(f"[+] Price < 1h MA60 ({ma60_1h:.4f}), 24h chg {change_pct:.2f}%")
    else:
        details.append(f"[-] Price vs 1h MA60 / 24h chg")

    ma5 = ind5m["ma5"]
    ma10 = ind5m["ma10"]
    ma30 = ind5m["ma30"]
    ema20 = ind5m["ema20"]
    if ma5 and ma10 and ma30 and ema20:
        bearish = ma5 < ma10 < ma30
        below_ema = price < ema20
        if bearish and below_ema:
            score += 1
            details.append(f"[+] 5m MAs stacked bearish, price < EMA20")
        else:
            details.append(f"[-] 5m MA structure not bearish")
    else:
        details.append(f"[-] 5m MAs insufficient data")

    candles5m = ind5m["candles"]
    if len(candles5m) >= 20:
        recent20 = candles5m[-20:]
        high20 = max(c["high"] for c in recent20)
        high24 = float(ticker.get("highPrice", price))
        ref_high = max(high20, high24)
        near_high = abs(price - ref_high) / price < 0.005
        last3 = candles5m[-3:]
        upper_wick = any((c["high"] - max(c["open"], c["close"])) > 0.5 * abs(c["close"] - c["open"]) for c in last3 if abs(c["close"] - c["open"]) > 0)
        if near_high and upper_wick:
            score += 1
            details.append(f"[+] Price near 20c/24h high with upper wicks")
        else:
            details.append(f"[-] Not near high with upper wicks")
    else:
        details.append(f"[-] Insufficient candle data for high check")

    k5, d5, j5 = ind5m["kdj"]
    if j5 is not None and j5 > 85:
        score += 1
        details.append(f"[+] 5m KDJ J={j5:.1f} > 85 (overbought)")
    else:
        j5_str = f"{j5:.1f}" if j5 is not None else "N/A"
        details.append(f"[-] 5m KDJ J={j5_str} not > 85")

    k1h, d1h, j1h = ind1h["kdj"]
    if j1h is not None and j1h > 50:
        score += 1
        details.append(f"[+] 1h KDJ J={j1h:.1f} > 50 (not oversold)")
    else:
        j1h_str = f"{j1h:.1f}" if j1h is not None else "N/A"
        details.append(f"[-] 1h KDJ J={j1h_str} not > 50")

    bids = depth.get("bids", [])
    asks = depth.get("asks", [])
    bid_vol = sum(float(b[1]) for b in bids) if bids else 0
    ask_vol = sum(float(a[1]) for a in asks) if asks else 0
    total_vol = bid_vol + ask_vol
    if total_vol > 0 and ask_vol / total_vol >= 0.6:
        score += 1
        details.append(f"[+] Sell depth {ask_vol/total_vol*100:.1f}% >= 60%")
    else:
        pct = (ask_vol / total_vol * 100) if total_vol > 0 else 0
        details.append(f"[-] Sell depth {pct:.1f}% < 60%")

    if asks and len(asks) >= 2:
        ask_sizes = [float(a[1]) for a in asks]
        avg_ask = sum(ask_sizes) / len(ask_sizes)
        max_ask = max(ask_sizes)
        max_ask_price = float(asks[ask_sizes.index(max_ask)][0])
        wall_close = abs(price - max_ask_price) / price < 0.002
        if avg_ask > 0 and max_ask >= 5 * avg_ask and wall_close:
            score += 1
            details.append(f"[+] Large ask wall {max_ask:.1f} >= 5x avg, within 0.2% of price")
        else:
            details.append(f"[-] No significant ask wall near price")
    else:
        details.append(f"[-] Insufficient ask data")

    vol_ma5 = ind5m["vol_ma5"]
    if vol_ma5 and len(candles5m) >= 5:
        last = candles5m[-1]
        recent5 = candles5m[-5:]
        green_candles_weak = [c for c in recent5 if c["close"] > c["open"] and c["vol"] < vol_ma5]
        large_red = last["close"] < last["open"] and last["vol"] >= 2 * vol_ma5
        if len(green_candles_weak) >= 2 or large_red:
            score += 1
            details.append(f"[+] Weak green vol or large red rejection candle")
        else:
            details.append(f"[-] No weak green / large red signal")
    else:
        details.append(f"[-] Insufficient volume data")

    vol_ma10 = ind5m["vol_ma10"]
    if len(candles5m) >= 1 and vol_ma10:
        last = candles5m[-1]
        if last["vol"] >= vol_ma10 and last["close"] < last["open"]:
            score += 1
            details.append(f"[+] Last candle vol >= MA10 and red")
        else:
            details.append(f"[-] Last candle vol/color not bearish")
    else:
        details.append(f"[-] Insufficient last candle data")

    funding = float(ticker.get("fundingRate", 0)) * 100
    if -0.005 <= funding <= 0.02:
        score += 1
        details.append(f"[+] Funding rate {funding:.4f}% in short-friendly range")
    else:
        details.append(f"[-] Funding rate {funding:.4f}% outside short-friendly range")

    if j15 is not None:
        if j15 > 70:
            score += 1
            details.append(f"[+] 15m KDJ J={j15:.1f} > 70 (momentum not oversold)")
        else:
            details.append(f"[-] 15m KDJ J={j15:.1f} not > 70")

    return score, details


def calc_trade_params_long(price, candles5m, symbol=""):
    if len(candles5m) >= 20:
        lows = [c["low"] for c in candles5m[-20:]]
        struct_low = min(lows)
    else:
        struct_low = price * 0.99
    floor = SYMBOL_SL_FLOORS.get(symbol, DEFAULT_SL_FLOOR)
    structural_sl_pct = (price - struct_low * 0.999) / price
    sl_pct = max(structural_sl_pct, floor)
    if sl_pct <= 0:
        sl_pct = floor
    sl = price * (1 - sl_pct)
    entry = price
    risk = entry - sl
    tp1 = entry + 1.5 * risk
    tp2 = entry + 2.0 * risk
    max_lev = 0.005 / sl_pct
    max_lev = min(max_lev, 100)
    print(f"[SL-L] {symbol}: structural={structural_sl_pct:.4f} floor={floor:.4f} final={sl_pct:.4f} max_lev={max_lev:.2f}x")
    return entry, sl, tp1, tp2, max_lev, sl_pct * 100


def calc_trade_params_short(price, candles5m, symbol=""):
    if len(candles5m) >= 20:
        highs = [c["high"] for c in candles5m[-20:]]
        struct_high = max(highs)
    else:
        struct_high = price * 1.01
    floor = SYMBOL_SL_FLOORS.get(symbol, DEFAULT_SL_FLOOR)
    structural_sl_pct = (struct_high * 1.001 - price) / price
    sl_pct = max(structural_sl_pct, floor)
    if sl_pct <= 0:
        sl_pct = floor
    sl = price * (1 + sl_pct)
    entry = price
    risk = sl - entry
    tp1 = entry - 1.5 * risk
    tp2 = entry - 2.0 * risk
    max_lev = 0.004 / sl_pct
    max_lev = min(max_lev, 100)
    print(f"[SL-S] {symbol}: structural={structural_sl_pct:.4f} floor={floor:.4f} final={sl_pct:.4f} max_lev={max_lev:.2f}x")
    return entry, sl, tp1, tp2, max_lev, sl_pct * 100


def log_alert(log_path, direction, symbol, score, trade, timestamp):
    entry, sl, tp1, tp2, max_lev, sl_pct = trade
    record = {
        "timestamp": timestamp,
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "leverage": round(max_lev, 1),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def print_alert_long(symbol, price, long_score, details, trade):
    entry, sl, tp1, tp2, max_lev, sl_pct = trade
    print(f"{GREEN}{BOLD}{'='*60}{RESET}")
    print(f"{GREEN}{BOLD}  LONG ALERT: {symbol}   Score: {long_score}/10{RESET}")
    print(f"{GREEN}{BOLD}{'='*60}{RESET}")
    print(f"{GREEN}  Price  : {price:.6g}{RESET}")
    print(f"{GREEN}  Entry  : {entry:.6g}{RESET}")
    print(f"{GREEN}  SL     : {sl:.6g}  ({sl_pct:.2f}% risk){RESET}")
    print(f"{GREEN}  TP1    : {tp1:.6g}  (1.5R){RESET}")
    print(f"{GREEN}  TP2    : {tp2:.6g}  (2.0R){RESET}")
    print(f"{GREEN}  Max Lev: {max_lev:.1f}x{RESET}")
    print(f"{DIM}  --- Checklist ---{RESET}")
    for d in details:
        color = GREEN if d.startswith("[+]") else DIM
        print(f"  {color}{d}{RESET}")
    print()


def print_alert_short(symbol, price, short_score, details, trade):
    entry, sl, tp1, tp2, max_lev, sl_pct = trade
    print(f"{RED}{BOLD}{'='*60}{RESET}")
    print(f"{RED}{BOLD}  SHORT ALERT: {symbol}   Score: {short_score}/10{RESET}")
    print(f"{RED}{BOLD}{'='*60}{RESET}")
    print(f"{RED}  Price  : {price:.6g}{RESET}")
    print(f"{RED}  Entry  : {entry:.6g}{RESET}")
    print(f"{RED}  SL     : {sl:.6g}  ({sl_pct:.2f}% risk){RESET}")
    print(f"{RED}  TP1    : {tp1:.6g}  (1.5R){RESET}")
    print(f"{RED}  TP2    : {tp2:.6g}  (2.0R){RESET}")
    print(f"{RED}  Max Lev: {max_lev:.1f}x{RESET}")
    print(f"{DIM}  --- Checklist ---{RESET}")
    for d in details:
        color = RED if d.startswith("[+]") else DIM
        print(f"  {color}{d}{RESET}")
    print()


def scan_symbol(symbol, j5m_prev=None):
    candles5m = fetch_klines(symbol, "5m", 100)
    candles1h = fetch_klines(symbol, "1h", 100)
    ticker = fetch_ticker(symbol)
    depth = fetch_depth(symbol, 20)

    j15 = None
    k15 = d15 = None
    try:
        candles15m = fetch_klines(symbol, "15m", 50)
        k15, d15, j15 = kdj(candles15m)
    except Exception:
        pass

    price = float(ticker.get("lastPrice", 0))
    change_pct = float(ticker.get("riseFallRate", 0)) * 100

    ind5m = calc_indicators(candles5m)
    ind1h = calc_indicators(candles1h)

    long_score, long_details = score_long(price, ind5m, ind1h, ticker, depth, j15, j5m_prev=j5m_prev, symbol=symbol)
    short_score, short_details = score_short(price, ind5m, ind1h, ticker, depth, j15, j5m_prev=j5m_prev, symbol=symbol)

    trend = classify_trend(candles5m, candles1h, price)

    bids = depth.get("bids", [])
    asks = depth.get("asks", [])
    bid_vol  = sum(float(b[1]) for b in bids) if bids else 0
    ask_vol  = sum(float(a[1]) for a in asks) if asks else 0
    total_vol = bid_vol + ask_vol

    best_bid  = float(bids[0][0]) if bids else None
    best_ask  = float(asks[0][0]) if asks else None
    spread    = round(best_ask - best_bid, 8) if (best_bid and best_ask) else None
    spread_pct = round(spread / best_bid * 100, 4) if (spread and best_bid) else None

    # Cumulative depth within 0.5% and 1% of price
    def _cvol(levels, within):
        return round(sum(float(l[1]) for l in levels
                         if price > 0 and abs(price - float(l[0])) / price <= within), 3)

    bid_vol_05p = _cvol(bids, 0.005)
    ask_vol_05p = _cvol(asks, 0.005)
    bid_vol_1p  = _cvol(bids, 0.010)
    ask_vol_1p  = _cvol(asks, 0.010)

    # Wall detection: largest level ≥ 3× average
    bid_sizes = [float(b[1]) for b in bids]
    ask_sizes = [float(a[1]) for a in asks]
    avg_bid = sum(bid_sizes) / len(bid_sizes) if bid_sizes else 0
    avg_ask = sum(ask_sizes) / len(ask_sizes) if ask_sizes else 0

    bid_wall = None
    if bid_sizes:
        max_bid = max(bid_sizes)
        if avg_bid > 0 and max_bid >= 3 * avg_bid:
            mi = bid_sizes.index(max_bid)
            bp_price = float(bids[mi][0])
            bid_wall = {
                "price": bp_price, "size": round(max_bid, 3),
                "dist_pct": round((price - bp_price) / price * 100, 3),
                "ratio": round(max_bid / avg_bid, 1),
            }

    ask_wall = None
    if ask_sizes:
        max_ask = max(ask_sizes)
        if avg_ask > 0 and max_ask >= 3 * avg_ask:
            mi = ask_sizes.index(max_ask)
            ak_price = float(asks[mi][0])
            ask_wall = {
                "price": ak_price, "size": round(max_ask, 3),
                "dist_pct": round((ak_price - price) / price * 100, 3),
                "ratio": round(max_ask / avg_ask, 1),
            }

    ind5m_summary = _ind_summary(ind5m)
    ind1h_summary = _ind_summary(ind1h)

    bid_pct = round(bid_vol / total_vol * 100, 1) if total_vol > 0 else None
    ask_pct = round(ask_vol / total_vol * 100, 1) if total_vol > 0 else None

    extra = {
        "ind5m": ind5m_summary,
        "ind1h": ind1h_summary,
        "k15": k15, "d15": d15, "j15": j15,
        "funding_rate": round(float(ticker.get("fundingRate", 0)) * 100, 6),
        "high_price": float(ticker.get("highPrice", price)),
        "low_price":  float(ticker.get("lowPrice",  price)),
        "volume_24":  float(ticker.get("amount24", ticker.get("vol24", 0))),
        "bid_pct": bid_pct,
        "ask_pct": ask_pct,
        "last_candle_5m": candles5m[-1] if candles5m else None,
        "bids_top": [[float(b[0]), float(b[1])] for b in bids[:10]],
        "asks_top": [[float(a[0]), float(a[1])] for a in asks[:10]],
        "best_bid": best_bid, "best_ask": best_ask,
        "spread": spread, "spread_pct": spread_pct,
        "bid_vol_05p": bid_vol_05p, "ask_vol_05p": ask_vol_05p,
        "bid_vol_1p":  bid_vol_1p,  "ask_vol_1p":  ask_vol_1p,
        "bid_wall": bid_wall, "ask_wall": ask_wall,
    }

    # ── Per-symbol scan summary log ────────────────────────────────────────────
    j5_val  = ind5m_summary.get("j")
    j1h_val = ind1h_summary.get("j")
    net     = "mainnet" if _USE_MAINNET else "testnet"
    print(
        f"  [{net}] {symbol:<6}  price={price:<12.5g}  "
        f"L:{long_score}/11  S:{short_score}/11  trend={trend:<12}  "
        f"j5={f'{j5_val:.1f}' if j5_val is not None else 'None':<7}  "
        f"j15={f'{j15:.1f}' if j15 is not None else 'None':<7}  "
        f"j1h={f'{j1h_val:.1f}' if j1h_val is not None else 'None':<7}  "
        f"B%={f'{bid_pct:.1f}' if bid_pct is not None else 'None':<6}  "
        f"S%={f'{ask_pct:.1f}' if ask_pct is not None else 'None'}"
    )
    if j5_val is None:
        print(f"  [INFO] {symbol}: j5=None — {len(candles5m)} 5m candles fetched "
              f"(need ≥9 for KDJ).  KDJ column will show '—' on dashboard.")
    if bid_pct is None:
        print(f"  [INFO] {symbol}: bid_pct=None — empty order book "
              f"({len(bids)} bids, {len(asks)} asks).  B%/S% columns will show '—' on dashboard.")

    return price, change_pct, long_score, short_score, long_details, short_details, candles5m, trend, j15, extra


def review_alerts(log_path, symbol_filter=None, direction_filter=None):
    import os
    if not os.path.exists(log_path):
        print(f"{YELLOW}No alert log found at: {log_path}{RESET}")
        return

    records = []
    with open(log_path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"{YELLOW}  Warning: skipping malformed line {lineno}{RESET}")

    if symbol_filter:
        records = [r for r in records if r.get("symbol", "").upper() == symbol_filter.upper()]
    if direction_filter:
        records = [r for r in records if r.get("direction", "").upper() == direction_filter.upper()]

    if not records:
        print(f"{YELLOW}No alerts match the given filters.{RESET}")
        return

    col_ts   = 19
    col_sym  = 12
    col_dir  = 6
    col_scr  = 5
    col_prc  = 12
    col_sl   = 12
    col_tp1  = 12
    col_tp2  = 12
    col_lev  = 7

    header = (
        f"{'Timestamp':<{col_ts}}  "
        f"{'Symbol':<{col_sym}}  "
        f"{'Dir':<{col_dir}}  "
        f"{'Score':>{col_scr}}  "
        f"{'Entry':>{col_prc}}  "
        f"{'SL':>{col_sl}}  "
        f"{'TP1':>{col_tp1}}  "
        f"{'TP2':>{col_tp2}}  "
        f"{'Lev':>{col_lev}}"
    )
    sep = "─" * len(header)

    print(f"\n{CYAN}{BOLD}  Alert History Review{RESET}")
    if symbol_filter or direction_filter:
        filters = []
        if symbol_filter:
            filters.append(f"symbol={symbol_filter.upper()}")
        if direction_filter:
            filters.append(f"direction={direction_filter.upper()}")
        print(f"{DIM}  Filters: {', '.join(filters)}{RESET}")
    print(f"{DIM}  Log: {log_path}{RESET}\n")
    print(f"{DIM}{sep}{RESET}")
    print(f"{BOLD}{header}{RESET}")
    print(f"{DIM}{sep}{RESET}")

    for r in records:
        direction = r.get("direction", "")
        dir_color = GREEN if direction == "LONG" else RED
        score = r.get("score", 0)
        scr_color = GREEN if score >= 8 else (YELLOW if score >= 6 else WHITE)
        row = (
            f"{DIM}{r.get('timestamp', ''):<{col_ts}}{RESET}  "
            f"{WHITE}{BOLD}{r.get('symbol', ''):<{col_sym}}{RESET}  "
            f"{dir_color}{direction:<{col_dir}}{RESET}  "
            f"{scr_color}{score:>{col_scr}}{RESET}  "
            f"{r.get('entry', 0):>{col_prc}.6g}  "
            f"{r.get('sl', 0):>{col_sl}.6g}  "
            f"{r.get('tp1', 0):>{col_tp1}.6g}  "
            f"{r.get('tp2', 0):>{col_tp2}.6g}  "
            f"{r.get('leverage', 0):>{col_lev}.1f}x"
        )
        print(row)

    print(f"{DIM}{sep}{RESET}\n")

    total   = len(records)
    n_long  = sum(1 for r in records if r.get("direction", "").upper() == "LONG")
    n_short = sum(1 for r in records if r.get("direction", "").upper() == "SHORT")
    avg_score = sum(r.get("score", 0) for r in records) / total if total else 0

    print(f"  {BOLD}Total alerts : {WHITE}{total}{RESET}")
    print(f"  {BOLD}LONGs        : {GREEN}{n_long}{RESET}")
    print(f"  {BOLD}SHORTs       : {RED}{n_short}{RESET}")
    print(f"  {BOLD}Avg score    : {CYAN}{avg_score:.1f}/10{RESET}\n")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Hyperliquid Futures Market Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scanner.py                                    # use default symbols\n"
            "  python scanner.py DOGE_USDT LINK_USDT               # custom symbols\n"
            "  python scanner.py --interval 60 --threshold 8 BTC_USDT ETH_USDT\n"
            "  python scanner.py --review                           # show all past alerts\n"
            "  python scanner.py --review --symbol BTC_USDT         # filter by symbol\n"
            "  python scanner.py --review --direction LONG          # filter by direction\n"
        ),
    )
    parser.add_argument(
        "symbols",
        nargs="*",
        metavar="SYMBOL",
        help="Trading pairs to scan (e.g. DOGE_USDT LINK_USDT). Defaults to the built-in list when omitted.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        metavar="SECONDS",
        help="Poll interval in seconds, must be > 0 (default: 30)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=7,
        metavar="SCORE",
        help="Minimum score out of 10 to trigger an alert, 0–10 (default: 7)",
    )
    parser.add_argument(
        "--log-file",
        default="alerts.log",
        metavar="PATH",
        help="File path to append alert records as JSON lines (default: alerts.log)",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="Print a formatted table of all past alerts from the log file, then exit.",
    )
    parser.add_argument(
        "--symbol",
        metavar="SYMBOL",
        help="Filter --review output to a specific symbol (e.g. BTC_USDT)",
    )
    parser.add_argument(
        "--direction",
        choices=["LONG", "SHORT", "long", "short"],
        metavar="DIRECTION",
        help="Filter --review output by direction: LONG or SHORT",
    )
    args = parser.parse_args()
    if not args.review:
        if args.symbol:
            parser.error("--symbol can only be used together with --review")
        if args.direction:
            parser.error("--direction can only be used together with --review")
        if args.interval <= 0:
            parser.error(f"--interval must be a positive integer, got {args.interval}")
        if not (0 <= args.threshold <= 10):
            parser.error(f"--threshold must be between 0 and 10, got {args.threshold}")
    return args


def main():
    args = parse_args()
    log_path = args.log_file

    if args.review:
        review_alerts(
            log_path,
            symbol_filter=args.symbol,
            direction_filter=args.direction,
        )
        return

    symbols = [s.upper() for s in args.symbols] if args.symbols else DEFAULT_SYMBOLS
    interval = args.interval
    threshold = args.threshold

    print(f"{CYAN}{BOLD}Hyperliquid Futures Market Scanner Starting...{RESET}")
    print(f"{DIM}Symbols: {', '.join(symbols)}{RESET}")
    print(f"{DIM}Polling every {interval}s | Alert threshold: {threshold}/10{RESET}")
    print(f"{DIM}Alert log: {log_path}{RESET}\n")

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{CYAN}{BOLD}{'─'*60}{RESET}")
        print(f"{CYAN}{BOLD}  Scan cycle: {now}{RESET}")
        print(f"{CYAN}{BOLD}{'─'*60}{RESET}\n")

        status_lines = []

        for symbol in symbols:
            try:
                price, change_pct, long_score, short_score, long_details, short_details, candles5m = scan_symbol(symbol)

                alerted = False

                if long_score >= threshold:
                    trade = calc_trade_params_long(price, candles5m)
                    print_alert_long(symbol, price, long_score, long_details, trade)
                    log_alert(log_path, "LONG", symbol, long_score, trade, now)
                    alerted = True

                if short_score >= threshold:
                    trade = calc_trade_params_short(price, candles5m)
                    print_alert_short(symbol, price, short_score, short_details, trade)
                    log_alert(log_path, "SHORT", symbol, short_score, trade, now)
                    alerted = True

                if not alerted:
                    chg_color = GREEN if change_pct >= 0 else RED
                    chg_str = f"{chg_color}{change_pct:+.2f}%{RESET}"
                    l_color = GREEN if long_score >= 5 else WHITE
                    s_color = RED if short_score >= 5 else WHITE
                    status_lines.append(
                        f"  {WHITE}{BOLD}{symbol:<12}{RESET} "
                        f"${price:<12.6g} "
                        f"{chg_str:<20} "
                        f"L:{l_color}{long_score}/10{RESET}  "
                        f"S:{s_color}{short_score}/10{RESET}"
                    )

            except Exception as e:
                status_lines.append(f"  {YELLOW}{symbol}: ERROR — {e}{RESET}")

        if status_lines:
            print(f"{DIM}  {'Symbol':<12} {'Price':<13} {'24h Chg':<12} {'Long':>6}  {'Short':>6}{RESET}")
            for line in status_lines:
                print(line)
            print()

        print(f"{DIM}  Next scan in {interval}s...{RESET}\n")
        time.sleep(interval)


if __name__ == "__main__":
    main()
