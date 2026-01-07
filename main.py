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
LAST_ANALYSIS = {}

PAIR_STATS = {}
TOTAL_PNL = 0.0
DEALS = 0
WIN_TRADES = 0
LOSS_TRADES = 0
GROSS_PROFIT = 0.0
GROSS_LOSS = 0.0

GRIDS_STARTED = 0
GRIDS_REJECTED = 0
ORDERS_TOTAL = 0
ORDERS_FILTERED = 0

MAX_EQUITY = DEPOSIT
MAX_DRAWDOWN = 0.0

# ================== STATE IO ==================
def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "ACTIVE_PAIRS": ACTIVE_PAIRS,
            "TOTAL_PNL": TOTAL_PNL,
            "DEALS": DEALS,
            "WIN_TRADES": WIN_TRADES,
            "LOSS_TRADES": LOSS_TRADES,
            "GROSS_PROFIT": GROSS_PROFIT,
            "GROSS_LOSS": GROSS_LOSS,
            "GRIDS_STARTED": GRIDS_STARTED,
            "GRIDS_REJECTED": GRIDS_REJECTED,
            "ORDERS_TOTAL": ORDERS_TOTAL,
            "ORDERS_FILTERED": ORDERS_FILTERED,
            "MAX_EQUITY": MAX_EQUITY,
            "MAX_DRAWDOWN": MAX_DRAWDOWN
        }, f)

def load_state():
    global TOTAL_PNL, DEALS, WIN_TRADES, LOSS_TRADES
    global GROSS_PROFIT, GROSS_LOSS
    global GRIDS_STARTED, GRIDS_REJECTED
    global ORDERS_TOTAL, ORDERS_FILTERED
    global MAX_EQUITY, MAX_DRAWDOWN

    if not os.path.exists(STATE_FILE):
        return

    with open(STATE_FILE) as f:
        d = json.load(f)

    TOTAL_PNL = d.get("TOTAL_PNL", 0.0)
    DEALS = d.get("DEALS", 0)
    WIN_TRADES = d.get("WIN_TRADES", 0)
    LOSS_TRADES = d.get("LOSS_TRADES", 0)
    GROSS_PROFIT = d.get("GROSS_PROFIT", 0.0)
    GROSS_LOSS = d.get("GROSS_LOSS", 0.0)
    GRIDS_STARTED = d.get("GRIDS_STARTED", 0)
    GRIDS_REJECTED = d.get("GRIDS_REJECTED", 0)
    ORDERS_TOTAL = d.get("ORDERS_TOTAL", 0)
    ORDERS_FILTERED = d.get("ORDERS_FILTERED", 0)
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

# ================== BINANCE ==================
async def get_klines(symbol, limit=120):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            BINANCE_URL,
            params={"symbol": symbol, "interval": TIMEFRAME, "limit": limit}
        ) as r:
            data = await r.json()
            return data if isinstance(data, list) else []

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

    LAST_ANALYSIS[pair] = {
        "price": price,
        "ema7": e7,
        "ema25": e25,
        "atr_pct": (a / price * 100) if a else None
    }

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
    global ORDERS_TOTAL, ORDERS_FILTERED

    levels = 8
    rng = atr_val * 2.5
    low, high = price - rng, price + rng
    step = (high - low) / levels

    margin = DEPOSIT * MAX_MARGIN_PER_GRID
    notional = margin * LEVERAGE
    qty = (notional / price) / levels

    orders = []

    for i in range(levels):
        ORDERS_TOTAL += 1
        entry = low + step * i
        exit = entry + step

        exp = abs(exit - entry) * qty
        fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)

        if entry * qty < MIN_ORDER_NOTIONAL or exp - fees < MIN_EXPECTED_PNL:
            ORDERS_FILTERED += 1
            continue

        orders.append({"entry": entry, "exit": exit, "qty": qty, "open": False})

    if len(orders) < 3:
        return None

    return {
        "side": side,
        "low": low,
        "high": high,
        "orders": orders,
        "atr": atr_val
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

            grid = build_grid(res["price"], res["atr"], res["side"])
            if not grid:
                GRIDS_REJECTED += 1
                LAST_REJECT_REASON[pair] = "grid filtered"
                continue

            ACTIVE_GRIDS[pair] = grid
            GRIDS_STARTED += 1

        await asyncio.sleep(SCAN_INTERVAL)

# ================== COMMANDS ==================
@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    equity = DEPOSIT + TOTAL_PNL
    roi = (equity - DEPOSIT) / DEPOSIT * 100
    avg = TOTAL_PNL / DEALS if DEALS else 0
    pf = abs(GROSS_PROFIT / GROSS_LOSS) if GROSS_LOSS else float("inf")

    lines = [
        "📊 GRID BOT — FULL STATS",
        "",
        f"Equity: {equity:.2f}$ | ROI: {roi:.2f}%",
        f"Deposit: {DEPOSIT}$ x{LEVERAGE}",
        f"Max DD: {MAX_DRAWDOWN:.2f}%",
        "",
        f"Deals: {DEALS}",
        f"Avg PnL: {avg:.4f}$ | PF: {pf:.2f}",
        "",
        f"Grids active: {len(ACTIVE_GRIDS)}/{MAX_GRIDS}",
        f"Grids started: {GRIDS_STARTED}",
        f"Grids rejected: {GRIDS_REJECTED}",
        "",
        f"Orders total: {ORDERS_TOTAL}",
        f"Orders filtered: {ORDERS_FILTERED}",
        "",
        "Pairs:"
    ]

    for p in ACTIVE_PAIRS:
        a = LAST_ANALYSIS.get(p)
        r = LAST_REJECT_REASON.get(p, "OK")
        if a:
            lines.append(
                f"• {p}: ATR {a['atr_pct']:.2f}% | EMA7/25 "
                f"{'BULL' if a['ema7'] > a['ema25'] else 'BEAR'} | {r}"
            )
        else:
            lines.append(f"• {p}: no data")

    await msg.answer("\n".join(lines))

@dp.message(Command("why"))
async def cmd_why(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    lines = ["🤔 WHY NO GRIDS", ""]

    for p in ACTIVE_PAIRS:
        if p in ACTIVE_GRIDS:
            lines.append(f"{p}: grid active")
        else:
            lines.append(f"{p}: {LAST_REJECT_REASON.get(p, 'no signal')}")

    await msg.answer("\n".join(lines))

# ================== MAIN ==================
async def main():
    load_state()
    asyncio.create_task(grid_engine())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())