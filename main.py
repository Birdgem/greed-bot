import os
import json
import csv
import asyncio
import aiohttp
import time
from statistics import mean
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# ================== ENV ==================
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

STATE_FILE = "state.json"
TRADES_FILE = "trades.csv"

# ================== SETTINGS ==================
ALL_PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT",
    "BNBUSDT", "DOGEUSDT", "AVAXUSDT",
    "HUSDT", "CYSUSDT"
]

TIMEFRAME = "5m"
BINANCE_URL = "https://api.binance.com/api/v3/klines"

DEPOSIT = 100.0
LEVERAGE = 10
MAX_GRIDS = 2
MAX_MARGIN_PER_GRID = 0.10

MAKER_FEE = 0.0002
TAKER_FEE = 0.0004

ATR_PERIOD = 14
SCAN_INTERVAL = 20
HEARTBEAT_INTERVAL = 1800

MIN_ORDER_NOTIONAL = 5.0
MIN_EXPECTED_PNL = 0.05

# ================== STATE ==================
START_TS = time.time()

ACTIVE_PAIRS = ["BTCUSDT", "ETHUSDT"]
ACTIVE_GRIDS = {}
LAST_REJECT_REASON = {}

PAIR_STATS = {}

TOTAL_PNL = 0.0
DEALS = 0
WIN_TRADES = 0
LOSS_TRADES = 0
GROSS_PROFIT = 0.0
GROSS_LOSS = 0.0

MAX_EQUITY = DEPOSIT
MAX_DRAWDOWN = 0.0

# ================== STATE IO ==================
def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "ACTIVE_PAIRS": ACTIVE_PAIRS,
            "ACTIVE_GRIDS": ACTIVE_GRIDS,
            "PAIR_STATS": PAIR_STATS,
            "TOTAL_PNL": TOTAL_PNL,
            "DEALS": DEALS,
            "WIN_TRADES": WIN_TRADES,
            "LOSS_TRADES": LOSS_TRADES,
            "GROSS_PROFIT": GROSS_PROFIT,
            "GROSS_LOSS": GROSS_LOSS,
            "MAX_EQUITY": MAX_EQUITY,
            "MAX_DRAWDOWN": MAX_DRAWDOWN
        }, f)

def load_state():
    global ACTIVE_PAIRS, ACTIVE_GRIDS, PAIR_STATS
    global TOTAL_PNL, DEALS, WIN_TRADES, LOSS_TRADES
    global GROSS_PROFIT, GROSS_LOSS, MAX_EQUITY, MAX_DRAWDOWN

    if not os.path.exists(STATE_FILE):
        return

    with open(STATE_FILE) as f:
        d = json.load(f)

    ACTIVE_PAIRS = d.get("ACTIVE_PAIRS", ACTIVE_PAIRS)
    ACTIVE_GRIDS = d.get("ACTIVE_GRIDS", {})
    PAIR_STATS = d.get("PAIR_STATS", {})
    TOTAL_PNL = d.get("TOTAL_PNL", 0.0)
    DEALS = d.get("DEALS", 0)
    WIN_TRADES = d.get("WIN_TRADES", 0)
    LOSS_TRADES = d.get("LOSS_TRADES", 0)
    GROSS_PROFIT = d.get("GROSS_PROFIT", 0.0)
    GROSS_LOSS = d.get("GROSS_LOSS", 0.0)
    MAX_EQUITY = d.get("MAX_EQUITY", DEPOSIT)
    MAX_DRAWDOWN = d.get("MAX_DRAWDOWN", 0.0)

# ================== INDICATORS ==================
def ema(data, p):
    k = 2 / (p + 1)
    e = sum(data[:p]) / p
    for x in data[p:]:
        e = x * k + e * (1 - k)
    return e

def atr(highs, lows, closes):
    tr = []
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        ))
    return mean(tr[-ATR_PERIOD:]) if len(tr) >= ATR_PERIOD else None

def atr_mult(price, atr_val):
    pct = atr_val / price * 100
    if pct < 0.4: return 3.5
    if pct < 0.8: return 2.5
    return 1.8

def grid_levels(price, atr_val):
    pct = atr_val / price * 100
    if pct < 0.4: return 10
    if pct < 0.8: return 8
    return 6

# ================== BINANCE ==================
async def get_klines(symbol, limit=120):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            BINANCE_URL,
            params={"symbol": symbol, "interval": TIMEFRAME, "limit": limit}
        ) as r:
            d = await r.json()
            return d if isinstance(d, list) else []

# ================== ANALYSIS ==================
async def analyze_pair(pair):
    kl = await get_klines(pair)
    if len(kl) < 50:
        LAST_REJECT_REASON[pair] = "not enough candles"
        return None

    c = [float(k[4]) for k in kl]
    h = [float(k[2]) for k in kl]
    l = [float(k[3]) for k in kl]

    price = c[-1]
    e7 = ema(c, 7)
    e25 = ema(c, 25)
    a = atr(h, l, c)

    if not a:
        LAST_REJECT_REASON[pair] = "ATR unavailable"
        return None

    if price > e7 > e25:
        side = "LONG"
    elif price < e7 < e25:
        side = "SHORT"
    else:
        LAST_REJECT_REASON[pair] = "trend FLAT"
        return None

    return {"price": price, "side": side, "atr": a}

# ================== GRID ==================
def build_grid(price, atr_val, side):
    levels = grid_levels(price, atr_val)
    rng = atr_val * atr_mult(price, atr_val)

    low, high = (
        (price - rng, price + rng)
        if side == "LONG"
        else (price + rng, price - rng)
    )

    step = abs(high - low) / levels
    margin = DEPOSIT * MAX_MARGIN_PER_GRID
    notional = margin * LEVERAGE
    qty = (notional / price) / levels

    orders = []

    for i in range(levels):
        entry = low + step * i if side == "LONG" else low - step * i
        exit = entry + step if side == "LONG" else entry - step

        exp = abs(exit - entry) * qty
        fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)

        if entry * qty < MIN_ORDER_NOTIONAL:
            continue
        if exp - fees < MIN_EXPECTED_PNL:
            continue

        orders.append({"entry": entry, "exit": exit, "qty": qty, "open": False})

    if len(orders) < 3:
        LAST_REJECT_REASON["grid"] = "orders filtered"
        return None

    return {
        "side": side,
        "low": min(low, high),
        "high": max(low, high),
        "orders": orders,
        "atr": atr_val
    }

def calc_pnl(entry, exit, qty, side):
    gross = (exit - entry) * qty if side == "LONG" else (entry - exit) * qty
    fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)
    return gross - fees

# ================== ENGINE ==================
async def grid_engine():
    global TOTAL_PNL, DEALS, WIN_TRADES, LOSS_TRADES
    global GROSS_PROFIT, GROSS_LOSS, MAX_EQUITY, MAX_DRAWDOWN

    while True:
        for pair, g in list(ACTIVE_GRIDS.items()):
            kl = await get_klines(pair, limit=2)
            price = float(kl[-1][4])

            if pair not in ACTIVE_PAIRS or not (g["low"] <= price <= g["high"]):
                del ACTIVE_GRIDS[pair]
                continue

            for o in g["orders"]:
                if not o["open"]:
                    if (g["side"] == "LONG" and price <= o["entry"]) or \
                       (g["side"] == "SHORT" and price >= o["entry"]):
                        o["open"] = True
                else:
                    if (g["side"] == "LONG" and price >= o["exit"]) or \
                       (g["side"] == "SHORT" and price <= o["exit"]):

                        pnl = calc_pnl(o["entry"], o["exit"], o["qty"], g["side"])
                        TOTAL_PNL += pnl
                        DEALS += 1

                        if pnl > 0:
                            WIN_TRADES += 1
                            GROSS_PROFIT += pnl
                        else:
                            LOSS_TRADES += 1
                            GROSS_LOSS += pnl

                        equity = DEPOSIT + TOTAL_PNL
                        MAX_EQUITY = max(MAX_EQUITY, equity)
                        MAX_DRAWDOWN = min(
                            MAX_DRAWDOWN,
                            (equity - MAX_EQUITY) / MAX_EQUITY * 100
                        )

                        PAIR_STATS.setdefault(pair, {"pnl": 0.0, "deals": 0})
                        PAIR_STATS[pair]["pnl"] += pnl
                        PAIR_STATS[pair]["deals"] += 1

                        o["open"] = False
                        save_state()

        if len(ACTIVE_GRIDS) < MAX_GRIDS:
            for pair in ACTIVE_PAIRS:
                if pair in ACTIVE_GRIDS:
                    continue

                res = await analyze_pair(pair)
                if not res:
                    continue

                grid = build_grid(res["price"], res["atr"], res["side"])
                if not grid:
                    continue

                ACTIVE_GRIDS[pair] = grid
                save_state()

                if len(ACTIVE_GRIDS) >= MAX_GRIDS:
                    break

        await asyncio.sleep(SCAN_INTERVAL)

# ================== HEARTBEAT ==================
async def heartbeat():
    while True:
        equity = DEPOSIT + TOTAL_PNL
        roi = (equity - DEPOSIT) / DEPOSIT * 100
        pf = abs(GROSS_PROFIT / GROSS_LOSS) if GROSS_LOSS != 0 else float("inf")
        avg = TOTAL_PNL / DEALS if DEALS else 0
        wr = (WIN_TRADES / DEALS * 100) if DEALS else 0

        await bot.send_message(
            ADMIN_ID,
            f"📊 GRID BOT\n"
            f"Equity: {equity:.2f}$ | ROI: {roi:.2f}%\n"
            f"Avg PnL: {avg:.3f}$ | WinRate: {wr:.1f}% | PF: {pf:.2f}\n"
            f"Deals: {DEALS} | Max DD: {MAX_DRAWDOWN:.2f}%\n"
            f"Active grids: {len(ACTIVE_GRIDS)}/{MAX_GRIDS}"
        )
        save_state()
        await asyncio.sleep(HEARTBEAT_INTERVAL)

# ================== COMMANDS ==================
@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    lines = ["📊 GRID BOT STATUS", ""]

    if ACTIVE_GRIDS:
        for p, g in ACTIVE_GRIDS.items():
            open_o = sum(1 for o in g["orders"] if o["open"])
            lines.append(
                f"• {p} {g['side']} | {open_o}/{len(g['orders'])} open "
                f"| {g['low']:.6f} → {g['high']:.6f} | ATR {g['atr']:.6f}"
            )
    else:
        lines.append("No active grids")

    await msg.answer("\n".join(lines))

@dp.message(Command("why"))
async def cmd_why(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    lines = ["🤔 WHY NO TRADES", ""]

    for p in ACTIVE_PAIRS:
        if p in ACTIVE_GRIDS:
            lines.append(f"{p}: grid active")
        else:
            lines.append(f"{p}: {LAST_REJECT_REASON.get(p, 'no signal yet')}")

    await msg.answer("\n".join(lines))

# ================== MAIN ==================
async def main():
    load_state()
    asyncio.create_task(grid_engine())
    asyncio.create_task(heartbeat())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())