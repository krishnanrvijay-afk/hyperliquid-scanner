"""
hyperliquid_api.py — Hyperliquid Perpetuals API module

Handles: balance, positions, order placement, SL/TP updates, position monitoring.
Uses hyperliquid-python-sdk with testnet endpoint by default.
Private key is read from HL_PRIVATE_KEY environment variable.

Testnet endpoint : https://api.hyperliquid-testnet.xyz
Mainnet endpoint : https://api.hyperliquid.xyz

Coin naming: Hyperliquid uses bare coin names ("ETH", "BTC", "SOL"),
not "_USDT" suffixes. The helper _to_coin() strips the suffix automatically.
"""

import os
import time
import json
import threading
import math

# ── Config from environment ───────────────────────────────────────────────────

HL_PRIVATE_KEY = os.environ.get("HL_PRIVATE_KEY", "")
HL_ADDRESS     = os.environ.get("HL_ADDRESS", "")     # optional override
HL_DRY_RUN     = os.environ.get("HL_DRY_RUN", "true").lower() != "false"
_USE_MAINNET   = os.environ.get("HL_USE_MAINNET", "false").lower() == "true"

TESTNET_URL    = "https://api.hyperliquid-testnet.xyz"
MAINNET_URL    = "https://api.hyperliquid.xyz"
BASE_URL       = MAINNET_URL if _USE_MAINNET else TESTNET_URL

_FEE_BUFFER    = 0.0012     # 0.12% — covers maker/taker + slippage buffer


# ── SDK helpers (lazy imports so import errors surface at call time) ───────────

def _wallet():
    """Return an eth_account LocalAccount from HL_PRIVATE_KEY."""
    if not HL_PRIVATE_KEY:
        raise RuntimeError("HL_PRIVATE_KEY environment variable is not set")
    from eth_account import Account
    key = HL_PRIVATE_KEY if HL_PRIVATE_KEY.startswith("0x") else "0x" + HL_PRIVATE_KEY
    return Account.from_key(key)


def _address() -> str:
    """Return the wallet address (from env override or derived from private key)."""
    if HL_ADDRESS:
        return HL_ADDRESS
    return _wallet().address


def _info():
    """Return a configured hyperliquid.Info instance (read-only)."""
    from hyperliquid.info import Info
    return Info(BASE_URL, skip_ws=True)


def _exchange():
    """Return a configured hyperliquid.Exchange instance (for signing orders)."""
    from hyperliquid.exchange import Exchange
    return Exchange(_wallet(), BASE_URL)


def _to_coin(symbol: str) -> str:
    """Convert 'ETH_USDT' or 'ETH-USD' to 'ETH' (Hyperliquid coin name)."""
    return symbol.replace("_USDT", "").replace("-USD", "").replace("-PERP", "").upper()


def _fmt(val, dp=6):
    return f"{val:.{dp}f}" if val is not None else "—"


# ── Account ───────────────────────────────────────────────────────────────────

def get_balance() -> float:
    """
    Return available USDT (withdrawable) balance from margin summary.
    Returns 0.0 on error.
    """
    try:
        address = _address()
        info    = _info()
        state   = info.user_state(address)
        margin  = state.get("marginSummary", {})
        # accountValue = total equity; withdrawable = free margin
        withdrawable = float(state.get("withdrawable", margin.get("accountValue", 0)))
        print(f"[hl] get_balance: withdrawable={withdrawable:.2f} USDT  address={address}")
        return withdrawable
    except Exception as exc:
        print(f"[hl] get_balance ERROR: {exc}")
        return 0.0


def get_user_state() -> dict:
    """Return full user state dict (positions, margin summary, etc.)."""
    try:
        return _info().user_state(_address())
    except Exception as exc:
        print(f"[hl] get_user_state ERROR: {exc}")
        return {}


# ── Positions ─────────────────────────────────────────────────────────────────

def get_open_positions(symbol: str = None) -> list:
    """
    Return list of open positions.
    Each position dict contains: coin, szi (size), entryPx, unrealizedPnl,
    leverage, positionValue, returnOnEquity, liquidationPx, marginUsed.
    Optionally filter by symbol (e.g. 'ETH_USDT' or 'ETH').
    """
    try:
        state     = get_user_state()
        positions = state.get("assetPositions", [])
        result    = []
        for ap in positions:
            pos  = ap.get("position", {})
            szi  = float(pos.get("szi", 0))
            if szi == 0:
                continue    # flat position — skip
            coin = pos.get("coin", "")
            if symbol and _to_coin(symbol) != coin:
                continue
            result.append({
                "coin":            coin,
                "size":            szi,
                "side":            "LONG" if szi > 0 else "SHORT",
                "entry_price":     float(pos.get("entryPx") or 0),
                "unrealized_pnl":  float(pos.get("unrealizedPnl") or 0),
                "leverage":        pos.get("leverage", {}).get("value", 1),
                "position_value":  float(pos.get("positionValue") or 0),
                "liquidation_px":  pos.get("liquidationPx"),
                "margin_used":     float(pos.get("marginUsed") or 0),
                "return_on_equity": float(pos.get("returnOnEquity") or 0),
                "_raw": pos,
            })
        return result
    except Exception as exc:
        print(f"[hl] get_open_positions ERROR: {exc}")
        return []


def get_position(symbol: str) -> dict | None:
    """Return the single open position for symbol, or None if flat."""
    positions = get_open_positions(symbol)
    return positions[0] if positions else None


# ── Orders ────────────────────────────────────────────────────────────────────

def _sz_for_margin(entry_price: float, margin_usdt: float, leverage: int) -> float:
    """Calculate order size (in coin units) from margin and leverage."""
    notional = margin_usdt * leverage
    sz = notional / entry_price
    return round(sz, 6)


def place_order_from_alert(
    symbol:       str,
    direction:    str,      # "LONG" or "SHORT"
    entry_price:  float,
    sl_price:     float,
    tp1_price:    float,
    tp2_price:    float,
    margin_usdt:  float,
    leverage:     int,
    dry_run:      bool = True,
) -> dict:
    """
    Place a futures order on Hyperliquid from a scanner alert.

    Parameters
    ----------
    symbol      : e.g. "ETH_USDT" or "ETH"
    direction   : "LONG" or "SHORT"
    entry_price : limit entry price
    sl_price    : stop-loss price (used to place a trigger SL order)
    tp1_price   : take-profit 1 price (limit TP order at 70% of size)
    tp2_price   : take-profit 2 price (limit TP order at remaining 30%)
    margin_usdt : collateral in USDT
    leverage    : leverage multiplier
    dry_run     : if True, log intent but do NOT send to exchange

    Returns
    -------
    dict with keys: success, dry_run, entry_order, sl_order, tp1_order, tp2_order
    """
    coin    = _to_coin(symbol)
    is_buy  = direction.upper() == "LONG"
    sz      = _sz_for_margin(entry_price, margin_usdt, leverage)
    sz_tp1  = round(sz * 0.70, 6)   # 70% at TP1
    sz_tp2  = round(sz * 0.30, 6)   # 30% at TP2

    print(f"[hl] place_order_from_alert ── {'DRY RUN' if dry_run else 'LIVE'} ──")
    print(f"[hl]   Coin      : {coin}")
    print(f"[hl]   Direction : {direction}")
    print(f"[hl]   Entry     : {_fmt(entry_price)}")
    print(f"[hl]   SL        : {_fmt(sl_price)}")
    print(f"[hl]   TP1       : {_fmt(tp1_price)}  ({sz_tp1:.6f} {coin})")
    print(f"[hl]   TP2       : {_fmt(tp2_price)}  ({sz_tp2:.6f} {coin})")
    print(f"[hl]   Margin    : ${margin_usdt:.2f}  Lev: {leverage}x  Size: {sz:.6f} {coin}")
    print(f"[hl]   Notional  : ${margin_usdt * leverage:.2f}")
    print(f"[hl]   Base URL  : {BASE_URL}")

    result = {
        "success":     False,
        "dry_run":     dry_run,
        "coin":        coin,
        "direction":   direction,
        "size":        sz,
        "entry_order": None,
        "sl_order":    None,
        "tp1_order":   None,
        "tp2_order":   None,
    }

    if dry_run:
        print(f"[hl]   → DRY RUN: no order sent")
        result["success"] = True
        result["message"] = "dry_run — no order sent"
        return result

    try:
        ex = _exchange()

        # 1. Set leverage (isolated)
        lev_resp = ex.update_leverage(leverage, coin, is_cross=False)
        print(f"[hl]   set_leverage → {lev_resp}")

        # 2. Entry limit order
        entry_resp = ex.order(
            coin, is_buy, sz, entry_price,
            {"limit": {"tif": "Gtc"}},
            reduce_only=False,
        )
        print(f"[hl]   entry_order → {entry_resp}")
        result["entry_order"] = entry_resp

        # 3. Stop-loss trigger order (reduce_only, opposite side)
        sl_trigger = {
            "trigger": {
                "isMarket":    True,
                "triggerPx":   str(sl_price),
                "tpsl":        "sl",
            }
        }
        sl_resp = ex.order(
            coin, not is_buy, sz, sl_price,
            sl_trigger,
            reduce_only=True,
        )
        print(f"[hl]   sl_order → {sl_resp}")
        result["sl_order"] = sl_resp

        # 4. TP1 limit order (70%)
        tp1_resp = ex.order(
            coin, not is_buy, sz_tp1, tp1_price,
            {"limit": {"tif": "Gtc"}},
            reduce_only=True,
        )
        print(f"[hl]   tp1_order → {tp1_resp}")
        result["tp1_order"] = tp1_resp

        # 5. TP2 limit order (30%)
        tp2_resp = ex.order(
            coin, not is_buy, sz_tp2, tp2_price,
            {"limit": {"tif": "Gtc"}},
            reduce_only=True,
        )
        print(f"[hl]   tp2_order → {tp2_resp}")
        result["tp2_order"] = tp2_resp

        result["success"] = True

    except Exception as exc:
        print(f"[hl]   place_order_from_alert ERROR: {exc}")
        result["error"] = str(exc)

    return result


# ── Position management ───────────────────────────────────────────────────────

def close_partial(symbol: str, pct: float, limit_price: float = None) -> dict:
    """
    Close a percentage of the open position for symbol.
    pct: 0.0–1.0  (e.g. 0.70 = close 70%)
    If limit_price is None, uses a market order (IOC at best bid/ask).
    """
    coin = _to_coin(symbol)
    pos  = get_position(symbol)
    if not pos:
        return {"success": False, "message": f"No open position for {symbol}"}

    total_sz = abs(pos["size"])
    close_sz = round(total_sz * pct, 6)
    is_long  = pos["size"] > 0
    is_buy   = not is_long    # closing a long = sell; closing a short = buy
    price    = limit_price or pos["entry_price"]   # fallback to entry as a rough limit

    print(f"[hl] close_partial: {pct*100:.0f}% of {coin} {pos['side']} pos  sz={close_sz:.6f}")

    if HL_DRY_RUN:
        print(f"[hl]   → DRY RUN: no order sent")
        return {"success": True, "dry_run": True, "message": "dry_run — no order sent"}

    try:
        ex = _exchange()
        order_type = {"limit": {"tif": "Ioc"}} if limit_price is None else {"limit": {"tif": "Gtc"}}
        resp = ex.order(coin, is_buy, close_sz, price, order_type, reduce_only=True)
        print(f"[hl]   close_partial → {resp}")
        return {"success": True, "response": resp}
    except Exception as exc:
        print(f"[hl]   close_partial ERROR: {exc}")
        return {"success": False, "error": str(exc)}


def update_sl(symbol: str, sl_price: float) -> dict:
    """
    Cancel existing stop-loss orders for symbol and place a new SL trigger at sl_price.
    Works by cancelling all open orders of the SL type, then placing a fresh one.
    """
    coin = _to_coin(symbol)
    pos  = get_position(symbol)
    if not pos:
        return {"success": False, "message": f"No open position for {symbol}"}

    is_long = pos["size"] > 0
    is_buy  = not is_long   # SL closes the position
    sz      = abs(pos["size"])

    print(f"[hl] update_sl: {coin} new SL={_fmt(sl_price)}")

    if HL_DRY_RUN:
        print(f"[hl]   → DRY RUN: no order sent")
        return {"success": True, "dry_run": True, "message": "dry_run — no order sent"}

    try:
        ex   = _exchange()
        info = _info()

        # Cancel all open orders for this coin first
        open_orders = info.open_orders(_address())
        cancelled   = 0
        for o in open_orders:
            if o.get("coin") == coin:
                cancel_resp = ex.cancel(coin, o["oid"])
                print(f"[hl]   cancelled oid={o['oid']} → {cancel_resp}")
                cancelled += 1

        # Place new SL trigger
        sl_trigger = {
            "trigger": {
                "isMarket":  True,
                "triggerPx": str(sl_price),
                "tpsl":      "sl",
            }
        }
        resp = ex.order(coin, is_buy, sz, sl_price, sl_trigger, reduce_only=True)
        print(f"[hl]   new SL order → {resp}  (cancelled {cancelled} prior orders)")
        return {"success": True, "cancelled": cancelled, "response": resp}

    except Exception as exc:
        print(f"[hl]   update_sl ERROR: {exc}")
        return {"success": False, "error": str(exc)}


# ── Position monitor ──────────────────────────────────────────────────────────

_TRAIL_STEP_PCT = 0.005   # re-trail SL if price moves 0.5% in our favour

def monitor_position(
    symbol:      str,
    direction:   str,
    entry_price: float,
    sl_price:    float,
    tp1_price:   float,
    tp2_price:   float,
    leverage:    int,
    poll_s:      int  = 5,
    on_alert:    callable = None,
    cancel_event: "threading.Event | None" = None,
    dry_run:     bool = True,
) -> None:
    """
    Background position monitor. Runs in a daemon thread.

    Behaviour:
    - Polls position every poll_s seconds
    - Trails SL upward (LONG) / downward (SHORT) as price moves in profit
    - Closes 70% at TP1, lets remainder run to TP2
    - Calls on_alert(msg: str) for Telegram notifications if provided
    - Stops when position is flat, cancel_event is set, or TP2 is hit

    Parameters are logged but orders are gated by HL_DRY_RUN (or dry_run arg).
    """
    coin     = _to_coin(symbol)
    live     = not (dry_run or HL_DRY_RUN)
    is_long  = direction.upper() == "LONG"
    peak     = entry_price      # highest (LONG) / lowest (SHORT) price seen
    tp1_done = False
    trail_sl = sl_price

    def _notify(msg: str):
        print(f"[hl_monitor] {msg}")
        if on_alert:
            try:
                on_alert(msg)
            except Exception:
                pass

    _notify(
        f"Monitor START — {coin} {direction} entry={_fmt(entry_price)} "
        f"SL={_fmt(sl_price)} TP1={_fmt(tp1_price)} TP2={_fmt(tp2_price)} "
        f"lev={leverage}x  {'LIVE' if live else 'DRY RUN'}"
    )

    try:
        info = _info()
        while True:
            if cancel_event and cancel_event.is_set():
                _notify(f"Monitor CANCELLED — {coin} {direction}")
                break

            time.sleep(poll_s)

            # Fetch current price
            try:
                mids = info.all_mids()
                price = float(mids.get(coin, 0))
            except Exception as exc:
                print(f"[hl_monitor] price fetch error: {exc}")
                continue

            if price == 0:
                continue

            # Check if position is still open
            pos = get_position(symbol)
            if not pos:
                _notify(f"Monitor EXIT — {coin} {direction} position is flat at ${price:.4f}")
                break

            pnl = pos.get("unrealized_pnl", 0)
            _notify(
                f"  {coin} {direction} | price={_fmt(price,4)} | "
                f"PnL={pnl:+.2f} | SL={_fmt(trail_sl,4)} | peak={_fmt(peak,4)}"
            )

            # TP1 check — close 70% of position
            if not tp1_done:
                if (is_long and price >= tp1_price) or (not is_long and price <= tp1_price):
                    _notify(f"  TP1 HIT — closing 70% of {coin} at ${price:.4f}")
                    if live:
                        close_partial(symbol, 0.70, price)
                    tp1_done = True

            # TP2 check — close remaining 30%
            if tp1_done:
                if (is_long and price >= tp2_price) or (not is_long and price <= tp2_price):
                    _notify(f"  TP2 HIT — closing remaining 30% of {coin} at ${price:.4f}")
                    if live:
                        close_partial(symbol, 1.00, price)
                    _notify(f"Monitor COMPLETE — all targets hit for {coin} {direction}")
                    break

            # Trailing SL
            if is_long:
                if price > peak + peak * _TRAIL_STEP_PCT:
                    peak = price
                    new_sl = entry_price + (price - entry_price) * 0.5   # trail to 50% of move
                    if new_sl > trail_sl:
                        _notify(f"  TRAIL SL → {_fmt(new_sl,4)}  (was {_fmt(trail_sl,4)})")
                        trail_sl = new_sl
                        if live:
                            update_sl(symbol, trail_sl)

                # SL breach
                if price <= trail_sl:
                    _notify(f"  SL HIT — {coin} {direction} stopped at ${price:.4f}")
                    break

            else:  # SHORT
                if price < peak - peak * _TRAIL_STEP_PCT:
                    peak = price
                    new_sl = entry_price - (entry_price - price) * 0.5
                    if new_sl < trail_sl:
                        _notify(f"  TRAIL SL → {_fmt(new_sl,4)}  (was {_fmt(trail_sl,4)})")
                        trail_sl = new_sl
                        if live:
                            update_sl(symbol, trail_sl)

                # SL breach
                if price >= trail_sl:
                    _notify(f"  SL HIT — {coin} {direction} stopped at ${price:.4f}")
                    break

    except Exception as exc:
        _notify(f"Monitor ERROR: {exc}")
