# ====== FULL FILE main.py (FIXED & BASE) ======

import os
import json
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

MIN_ORDER_NOTIONAL = 5.0
BASE_MIN_PNL = 0.02  # <<< ВАЖНЫЙ ФИКС

# ================== STATE ==================
START_TS = time.time()

ACTIVE_PAIRS = ["BTCUSDT", "ETHUSDT"]
ACTIVE_GRIDS = {}

PAIR_STATE = {}          # WAIT / FLAT / GRID / FILTERED
PAIR_DEBUG = {}          # подробная причина

TOTAL_PNL = 0.0
DEALS = 0

GRIDS_STARTED = 0
GRIDS_REJECTED = 0

ORDERS_TOTAL = 0
ORDERS_FILTERED = 0

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
        PAIR_STATE[pair] = "WAIT"
        PAIR_DEBUG[pair] = "not enough candles"
        return None

    c = [float(k[4]) for k in kl]
    h = [float(k[2]) for k in kl]
    l = [float(k[3]) for k in kl]

    price = c[-1]
    e7 = ema(c, 7)
    e25 = ema(c, 25)
    a = atr(h, l, c)

    if not a:
        PAIR_STATE[pair] = "WAIT"
        PAIR_DEBUG[pair] = "ATR unavailable"
        return None

    atr_pct = a / price * 100

    if price > e7 > e25:
        side = "LONG"
    elif price < e7 < e25:
        side = "SHORT"
    else:
        PAIR_STATE[pair] = "FLAT"
        PAIR_DEBUG[pair] = f"EMA flat | ATR {atr_pct:.2f}%"
        return None

    return {
        "price": price,
        "side": side,
        "atr": a,
        "atr_pct": atr_pct
    }

# ================== GRID ==================
def build_grid(pair, price, atr_val, atr_pct, side):
    global ORDERS_TOTAL, ORDERS_FILTERED

    levels = 8
    rng = atr_val * 2.5
    low = price - rng
    high = price + rng
    step = (high - low) / levels

    margin = DEPOSIT * MAX_MARGIN_PER_GRID
    notional = margin * LEVERAGE
    qty = (notional / price) / levels

    # динамический минимум профита
    min_pnl = max(BASE_MIN_PNL, atr_pct * 0.01)

    orders = []

    for i in range(levels):
        ORDERS_TOTAL += 1

        entry = low + step * i
        exit = entry + step
        exp = (exit - entry) * qty
        fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)

        if entry * qty < MIN_ORDER_NOTIONAL or exp - fees < min_pnl:
            ORDERS_FILTERED += 1
            continue

        orders.append({"entry": entry, "exit": exit, "qty": qty, "open": False})

    if len(orders) < 3:
        PAIR_STATE[pair] = "FILTERED"
        PAIR_DEBUG[pair] = f"orders filtered ({len(orders)}) | ATR {atr_pct:.2f}%"
        return None

    PAIR_STATE[pair] = "GRID"
    PAIR_DEBUG[pair] = f"{side} | ATR {atr_pct:.2f}% | orders {len(orders)}"

    return {
        "side": side,
        "low": low,
        "high": high,
        "orders": orders
    }

# ================== ENGINE ==================
async def grid_engine():
    global GRIDS_STARTED, GRIDS_REJECTED

    while True:
        for pair in ACTIVE_PAIRS:
            if pair in ACTIVE_GRIDS:
                continue

            res = await analyze_pair(pair)
            if not res:
                continue

            grid = build_grid(
                pair,
                res["price"],
                res["atr"],
                res["atr_pct"],
                res["side"]
            )

            if not grid:
                GRIDS_REJECTED += 1
                continue

            ACTIVE_GRIDS[pair] = grid
            GRIDS_STARTED += 1

        await asyncio.sleep(SCAN_INTERVAL)

# ================== COMMANDS ==================
@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    lines = [
        "📊 GRID BOT — CONTROL PANEL",
        "",
        f"Active grids: {len(ACTIVE_GRIDS)}/{MAX_GRIDS}",
        f"Grids started: {GRIDS_STARTED}",
        f"Grids rejected: {GRIDS_REJECTED}",
        "",
        f"Orders total: {ORDERS_TOTAL}",
        f"Orders filtered: {ORDERS_FILTERED}",
        "",
        "Pairs:"
    ]

    for p in ACTIVE_PAIRS:
        state = PAIR_STATE.get(p, "WAIT")
        info = PAIR_DEBUG.get(p, "-")
        lines.append(f"• {p}: {state} | {info}")

    await msg.answer("\n".join(lines))

@dp.message(Command("why"))
async def cmd_why(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    lines = ["🤔 WHY NO GRID", ""]
    for p in ACTIVE_PAIRS:
        lines.append(f"{p}: {PAIR_DEBUG.get(p, 'no data')}")
    await msg.answer("\n".join(lines))

# ================== MAIN ==================
async def main():
    asyncio.create_task(grid_engine())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())