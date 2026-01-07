# ====== FULL FILE main.py ======

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

def calc_pnl(entry, exit, qty, side):
    gross = (exit - entry) * qty if side == "LONG" else (entry - exit) * qty
    fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)
    return gross - fees

# ================== BINANCE ==================
async def get_klines(symbol, limit=120):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            BINANCE_URL,
            params={"symbol": symbol, "interval": TIMEFRAME, "limit": limit}
        ) as r:
            d = await r.json()
            return d if isinstance(d, list) else []

# ================== COMMANDS ==================
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

    if action == "add":
        if pair not in ACTIVE_PAIRS:
            ACTIVE_PAIRS.append(pair)
            save_state()
            await msg.answer(f"✅ {pair} added")
        else:
            await msg.answer("Already active")

    elif action == "remove":
        if pair in ACTIVE_PAIRS:
            ACTIVE_PAIRS.remove(pair)
            ACTIVE_GRIDS.pop(pair, None)
            save_state()
            await msg.answer(f"🛑 {pair} removed")
        else:
            await msg.answer("Not active")

# ================== ENGINE ==================
async def grid_engine():
    while True:
        await asyncio.sleep(SCAN_INTERVAL)

# ================== HEARTBEAT ==================
async def heartbeat():
    while True:
        uptime = int((time.time() - START_TS) / 60)
        await bot.send_message(
            ADMIN_ID,
            f"📡 GRID BOT\nUptime: {uptime} min\nActive pairs: {', '.join(ACTIVE_PAIRS)}"
        )
        await asyncio.sleep(HEARTBEAT_INTERVAL)

# ================== MAIN ==================
async def main():
    load_state()
    asyncio.create_task(grid_engine())
    asyncio.create_task(heartbeat())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())