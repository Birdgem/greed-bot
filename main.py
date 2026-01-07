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

PAIR_STATE = {}        # LIVE / FILTERED / FLAT / NO_ATR
PAIR_DEBUG = {}        # подробная диагностика

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
            "PAIR_STATS": PAIR_STATS,
            "TOTAL_PNL": TOTAL_PNL,
            "DEALS": DEALS,
            "WIN_TRADES": WIN_TRADES,
            "LOSS_TRADES": LOSS_TRADES,
            "GROSS_PROFIT": GROSS_PROFIT,
            "GROSS_LOSS": GROSS_LOSS,
            "MAX_EQUITY": MAX_EQUITY,
            "MAX_DRAWDOWN": MAX_DRAWDOWN,
            "GRIDS_STARTED": GRIDS_STARTED,
            "GRIDS_REJECTED": GRIDS_REJECTED,
            "ORDERS_TOTAL": ORDERS_TOTAL,
            "ORDERS_FILTERED": ORDERS_FILTERED
        }, f)

def load_state():
    global ACTIVE_PAIRS, PAIR_STATS
    global TOTAL_PNL, DEALS, WIN_TRADES, LOSS_TRADES
    global GROSS_PROFIT, GROSS_LOSS, MAX_EQUITY, MAX_DRAWDOWN
    global GRIDS_STARTED, GRIDS_REJECTED, ORDERS_TOTAL, ORDERS_FILTERED

    if not os.path.exists(STATE_FILE):
        return

    with open(STATE_FILE) as f:
        d = json.load(f)

    ACTIVE_PAIRS = d.get("ACTIVE_PAIRS", ACTIVE_PAIRS)
    PAIR_STATS = d.get("PAIR_STATS", {})
    TOTAL_PNL = d.get("TOTAL_PNL", 0.0)
    DEALS = d.get("DEALS", 0)
    WIN_TRADES = d.get("WIN_TRADES", 0)
    LOSS_TRADES = d.get("LOSS_TRADES", 0)
    GROSS_PROFIT = d.get("GROSS_PROFIT", 0.0)
    GROSS_LOSS = d.get("GROSS_LOSS", 0.0)
    MAX_EQUITY = d.get("MAX_EQUITY", DEPOSIT)
    MAX_DRAWDOWN = d.get("MAX_DRAWDOWN", 0.0)
    GRIDS_STARTED = d.get("GRIDS_STARTED", 0)
    GRIDS_REJECTED = d.get("GRIDS_REJECTED", 0)
    ORDERS_TOTAL = d.get("ORDERS_TOTAL", 0)
    ORDERS_FILTERED = d.get("ORDERS_FILTERED", 0)

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
        PAIR_STATE[pair] = "NO_DATA"
        PAIR_DEBUG[pair] = "not enough candles"
        return None

    c = [float(k[4]) for k in kl]
    h = [float(k[2]) for k in kl]
    l = [float(k[3]) for k in kl]

    price = c[-1]
    e7 = ema(c, 7)
    e25 = ema(c, 25)
    a = atr(h, l, c)

    atr_pct = a / price * 100 if a else 0

    if not a:
        PAIR_STATE[pair] = "NO_ATR"
        PAIR_DEBUG[pair] = f"ATR unavailable"
        return None

    if price > e7 > e25:
        side = "LONG"
        trend = "BULL"
    elif price < e7 < e25:
        side = "SHORT"
        trend = "BEAR"
    else:
        PAIR_STATE[pair] = "FLAT"
        PAIR_DEBUG[pair] = f"EMA7/25 FLAT | ATR {atr_pct:.2f}%"
        return None

    return {
        "price": price,
        "side": side,
        "atr": a,
        "atr_pct": atr_pct,
        "trend": trend
    }

# ================== GRID ==================
def build_grid(pair, data):
    global ORDERS_TOTAL, ORDERS_FILTERED, GRIDS_REJECTED

    price = data["price"]
    atr_val = data["atr"]

    levels = 10
    rng = atr_val * 2.5
    low = price - rng
    high = price + rng
    step = (high - low) / levels

    margin = DEPOSIT * MAX_MARGIN_PER_GRID
    notional = margin * LEVERAGE
    qty = (notional / price) / levels

    orders = []

    for i in range(levels):
        ORDERS_TOTAL += 1
        entry = low + step * i
        exit = entry + step
        exp = (exit - entry) * qty
        fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)

        if entry * qty < MIN_ORDER_NOTIONAL or exp - fees < MIN_EXPECTED_PNL:
            ORDERS_FILTERED += 1
            continue

        orders.append({"entry": entry, "exit": exit, "qty": qty, "open": False})

    if len(orders) < 3:
        GRIDS_REJECTED += 1
        PAIR_STATE[pair] = "FILTERED"
        PAIR_DEBUG[pair] = f"orders filtered ({len(orders)}) | ATR {data['atr_pct']:.2f}%"
        return None

    return {
        "side": data["side"],
        "low": low,
        "high": high,
        "orders": orders,
        "atr": data["atr_pct"]
    }

# ================== ENGINE ==================
async def grid_engine():
    global TOTAL_PNL, DEALS, WIN_TRADES, LOSS_TRADES
    global GROSS_PROFIT, GROSS_LOSS, MAX_EQUITY, MAX_DRAWDOWN
    global GRIDS_STARTED

    while True:
        # обновление активных сеток
        for pair, g in list(ACTIVE_GRIDS.items()):
            kl = await get_klines(pair, limit=2)
            price = float(kl[-1][4])

            if price < g["low"] or price > g["high"]:
                del ACTIVE_GRIDS[pair]
                continue

            for o in g["orders"]:
                if not o["open"] and price <= o["entry"]:
                    o["open"] = True
                elif o["open"] and price >= o["exit"]:
                    pnl = (o["exit"] - o["entry"]) * o["qty"]
                    TOTAL_PNL += pnl
                    DEALS += 1
                    WIN_TRADES += 1 if pnl > 0 else 0
                    LOSS_TRADES += 1 if pnl <= 0 else 0
                    GROSS_PROFIT += pnl if pnl > 0 else 0
                    GROSS_LOSS += pnl if pnl <= 0 else 0
                    o["open"] = False

        # запуск новых сеток
        if len(ACTIVE_GRIDS) < MAX_GRIDS:
            for pair in ACTIVE_PAIRS:
                if pair in ACTIVE_GRIDS:
                    continue

                data = await analyze_pair(pair)
                if not data:
                    continue

                grid = build_grid(pair, data)
                if not grid:
                    continue

                ACTIVE_GRIDS[pair] = grid
                PAIR_STATE[pair] = "ACTIVE"
                GRIDS_STARTED += 1

                if len(ACTIVE_GRIDS) >= MAX_GRIDS:
                    break

        save_state()
        await asyncio.sleep(SCAN_INTERVAL)

# ================== COMMANDS ==================
@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    equity = DEPOSIT + TOTAL_PNL
    roi = (equity - DEPOSIT) / DEPOSIT * 100
    pf = abs(GROSS_PROFIT / GROSS_LOSS) if GROSS_LOSS != 0 else float("inf")
    avg = TOTAL_PNL / DEALS if DEALS else 0

    lines = [
        "📊 GRID BOT — FULL STATS",
        "",
        f"Equity: {equity:.2f}$ | ROI: {roi:.2f}%",
        f"Deals: {DEALS} | Avg PnL: {avg:.4f}$ | PF: {pf:.2f}",
        f"Grids active: {len(ACTIVE_GRIDS)}/{MAX_GRIDS}",
        f"Grids started: {GRIDS_STARTED}",
        f"Grids rejected: {GRIDS_REJECTED}",
        f"Orders total: {ORDERS_TOTAL}",
        f"Orders filtered: {ORDERS_FILTERED}",
        "",
        "Pairs:"
    ]

    for p in ACTIVE_PAIRS:
        state = PAIR_STATE.get(p, "WAIT")
        dbg = PAIR_DEBUG.get(p, "")
        lines.append(f"• {p}: {state} | {dbg}")

    await msg.answer("\n".join(lines))

@dp.message(Command("pairs"))
async def cmd_pairs(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("Active pairs:\n" + "\n".join(ACTIVE_PAIRS))

@dp.message(Command("pair"))
async def cmd_pair(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = msg.text.split()
    if len(parts) != 3:
        await msg.answer("Usage: /pair add|remove SYMBOL")
        return

    action, pair = parts[1], parts[2].upper()
    if pair not in ALL_PAIRS:
        await msg.answer("Pair not allowed")
        return

    if action == "add" and pair not in ACTIVE_PAIRS:
        ACTIVE_PAIRS.append(pair)
        await msg.answer(f"{pair} added")
    elif action == "remove" and pair in ACTIVE_PAIRS:
        ACTIVE_PAIRS.remove(pair)
        ACTIVE_GRIDS.pop(pair, None)
        await msg.answer(f"{pair} removed")

# ================== MAIN ==================
async def main():
    load_state()
    asyncio.create_task(grid_engine())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())