# ====== FULL FILE main.py ======

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
            params={"symbol":symbol,"interval":TIMEFRAME,"limit":limit}
        ) as r:
            d = await r.json()
            return d if isinstance(d, list) else []

# ================== ANALYSIS ==================
async def analyze_pair(pair):
    kl = await get_klines(pair)
    if len(kl) < 50:
        LAST_REJECT_REASON[pair] = "NO_DATA"
        return None

    c = [float(k[4]) for k in kl]
    h = [float(k[2]) for k in kl]
    l = [float(k[3]) for k in kl]

    price = c[-1]
    e7 = ema(c, 7)
    e25 = ema(c, 25)
    a = atr(h, l, c)

    if not a:
        LAST_REJECT_REASON[pair] = "ATR_FAIL"
        return None

    # -------- MARKET MODE --------
    if price > e7 > e25:
        mode = "TREND_LONG"
        side = "LONG"
    elif price < e7 < e25:
        mode = "TREND_SHORT"
        side = "SHORT"
    else:
        mode = "RANGE"
        side = "BOTH"

    return {
        "price": price,
        "atr": a,
        "ema7": e7,
        "ema25": e25,
        "mode": mode,
        "side": side
    }

# ================== GRID BUILDERS ==================
def build_grid_trend(price, atr_val, side):
    rng = atr_val * 2.5
    levels = 8
    low = price - rng
    high = price + rng
    step = (high - low) / levels

    margin = DEPOSIT * MAX_MARGIN_PER_GRID
    notional = margin * LEVERAGE
    qty = (notional / price) / levels

    orders = []
    for i in range(levels):
        entry = low + step * i
        exit = entry + step
        orders.append({
            "entry": entry,
            "exit": exit,
            "qty": qty,
            "open": False
        })

    return {
        "mode": "TREND",
        "side": side,
        "low": low,
        "high": high,
        "orders": orders
    }

def build_grid_range(price, atr_val):
    rng = atr_val * 1.5
    levels = 10
    low = price - rng
    high = price + rng
    step = (high - low) / levels

    margin = DEPOSIT * MAX_MARGIN_PER_GRID
    notional = margin * LEVERAGE
    qty = (notional / price) / levels

    orders = []
    for i in range(levels):
        buy = low + step * i
        sell = buy + step
        orders.append({
            "buy": buy,
            "sell": sell,
            "qty": qty,
            "open": False
        })

    return {
        "mode": "RANGE",
        "side": "BOTH",
        "low": low,
        "high": high,
        "orders": orders
    }

# ================== ENGINE ==================
async def grid_engine():
    while True:
        # start grids
        if len(ACTIVE_GRIDS) < MAX_GRIDS:
            for pair in ACTIVE_PAIRS:
                if pair in ACTIVE_GRIDS:
                    continue

                res = await analyze_pair(pair)
                if not res:
                    continue

                if res["mode"] == "RANGE":
                    grid = build_grid_range(res["price"], res["atr"])
                else:
                    grid = build_grid_trend(res["price"], res["atr"], res["side"])

                ACTIVE_GRIDS[pair] = grid

        await asyncio.sleep(SCAN_INTERVAL)

# ================== COMMANDS ==================
@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    lines = ["📊 GRID BOT STATUS", ""]

    for p in ACTIVE_PAIRS:
        if p in ACTIVE_GRIDS:
            g = ACTIVE_GRIDS[p]
            lines.append(
                f"{p}: ACTIVE | {g['mode']} | range {g['low']:.4f} → {g['high']:.4f}"
            )
        else:
            reason = LAST_REJECT_REASON.get(p, "waiting")
            lines.append(f"{p}: NO GRID | {reason}")

    await msg.answer("\n".join(lines))

@dp.message(Command("pairs"))
async def cmd_pairs(msg: types.Message):
    if msg.from_user.id == ADMIN_ID:
        await msg.answer("Active pairs:\n" + "\n".join(ACTIVE_PAIRS))

# ================== MAIN ==================
async def main():
    asyncio.create_task(grid_engine())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())