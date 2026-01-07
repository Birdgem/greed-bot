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
PAIRS = ["SOLUSDT", "ETHUSDT", "BTCUSDT"]
TIMEFRAME = "5m"

BINANCE_URL = "https://api.binance.com/api/v3/klines"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

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
BOT_MODE = "ACTIVE"

ACTIVE_GRIDS = {}
PAIR_STATS = {}

TOTAL_PNL = 0.0
DAILY_PNL = 0.0
WEEKLY_PNL = 0.0
DEALS = 0

MAX_EQUITY = DEPOSIT
MAX_DRAWDOWN = 0.0

LAST_DAY = datetime.utcnow().date()
LAST_WEEK = datetime.utcnow().isocalendar().week

BTC_CONTEXT = "⚪ BTC FLAT"
FUNDING_CACHE = {}

# ================== STATE IO ==================
def save_state():
    data = {
        "BOT_MODE": BOT_MODE,
        "ACTIVE_GRIDS": ACTIVE_GRIDS,
        "PAIR_STATS": PAIR_STATS,
        "TOTAL_PNL": TOTAL_PNL,
        "DAILY_PNL": DAILY_PNL,
        "WEEKLY_PNL": WEEKLY_PNL,
        "DEALS": DEALS,
        "MAX_EQUITY": MAX_EQUITY,
        "MAX_DRAWDOWN": MAX_DRAWDOWN,
        "LAST_DAY": LAST_DAY.isoformat(),
        "LAST_WEEK": LAST_WEEK
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)

def load_state():
    global BOT_MODE, ACTIVE_GRIDS, PAIR_STATS
    global TOTAL_PNL, DAILY_PNL, WEEKLY_PNL, DEALS
    global MAX_EQUITY, MAX_DRAWDOWN, LAST_DAY, LAST_WEEK

    if not os.path.exists(STATE_FILE):
        return

    with open(STATE_FILE, "r") as f:
        data = json.load(f)

    BOT_MODE = data.get("BOT_MODE", "ACTIVE")
    ACTIVE_GRIDS = data.get("ACTIVE_GRIDS", {})
    PAIR_STATS = data.get("PAIR_STATS", {})

    TOTAL_PNL = data.get("TOTAL_PNL", 0.0)
    DAILY_PNL = data.get("DAILY_PNL", 0.0)
    WEEKLY_PNL = data.get("WEEKLY_PNL", 0.0)
    DEALS = data.get("DEALS", 0)

    MAX_EQUITY = data.get("MAX_EQUITY", DEPOSIT)
    MAX_DRAWDOWN = data.get("MAX_DRAWDOWN", 0.0)

    LAST_DAY = datetime.fromisoformat(data.get("LAST_DAY")).date()
    LAST_WEEK = data.get("LAST_WEEK")

# ================== INDICATORS ==================
def ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    e = sum(data[:period]) / period
    for p in data[period:]:
        e = p * k + e * (1 - k)
    return e

def atr(highs, lows, closes):
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)
    return mean(trs[-ATR_PERIOD:]) if len(trs) >= ATR_PERIOD else None

def atr_mult_by_vol(price, atr_val):
    atr_pct = (atr_val / price) * 100
    if atr_pct < 0.4:
        return 3.5
    elif atr_pct < 0.8:
        return 2.5
    return 1.8

def grid_levels_by_atr(price, atr_val):
    atr_pct = (atr_val / price) * 100
    if atr_pct < 0.4:
        return 10
    elif atr_pct < 0.8:
        return 8
    return 6

# ================== BINANCE ==================
async def get_klines(symbol, limit=120):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            BINANCE_URL,
            params={"symbol": symbol, "interval": TIMEFRAME, "limit": limit}
        ) as r:
            data = await r.json()
            return data if isinstance(data, list) else []

# ================== GRID ==================
def build_grid(price, atr_val):
    levels = grid_levels_by_atr(price, atr_val)
    rng = atr_val * atr_mult_by_vol(price, atr_val)

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

        pnl = (sell - buy) * qty
        fees = (buy * qty * MAKER_FEE) + (sell * qty * TAKER_FEE)

        if buy * qty < MIN_ORDER_NOTIONAL:
            continue
        if pnl - fees < MIN_EXPECTED_PNL:
            continue

        orders.append({"buy": buy, "sell": sell, "qty": qty, "open": False})

    if len(orders) < 3:
        return None, None, None, 0

    return low, high, orders, len(orders)

def calc_pnl(entry, exit, qty):
    return (exit - entry) * qty - (
        entry * qty * MAKER_FEE + exit * qty * TAKER_FEE
    )

# ================== ENGINE ==================
async def grid_engine():
    global TOTAL_PNL, DAILY_PNL, WEEKLY_PNL, DEALS
    global MAX_EQUITY, MAX_DRAWDOWN

    while True:
        for pair, g in list(ACTIVE_GRIDS.items()):
            kl = await get_klines(pair, limit=2)
            price = float(kl[-1][4])

            if price < g["low"] or price > g["high"]:
                del ACTIVE_GRIDS[pair]
                continue

            for o in g["orders"]:
                pnl = None

                if g["side"] == "LONG":
                    if not o["open"] and price <= o["buy"]:
                        o["open"] = True
                    elif o["open"] and price >= o["sell"]:
                        pnl = calc_pnl(o["buy"], o["sell"], o["qty"])
                        o["open"] = False

                if pnl is not None:
                    TOTAL_PNL += pnl
                    DEALS += 1

                    equity = DEPOSIT + TOTAL_PNL
                    MAX_EQUITY = max(MAX_EQUITY, equity)
                    dd = (equity - MAX_EQUITY) / MAX_EQUITY * 100
                    MAX_DRAWDOWN = min(MAX_DRAWDOWN, dd)

                    PAIR_STATS.setdefault(pair, {"pnl": 0, "deals": 0})
                    PAIR_STATS[pair]["pnl"] += pnl
                    PAIR_STATS[pair]["deals"] += 1

                    save_state()

        await asyncio.sleep(SCAN_INTERVAL)

# ================== HEARTBEAT ==================
async def heartbeat():
    while True:
        equity = DEPOSIT + TOTAL_PNL
        roi = (equity - DEPOSIT) / DEPOSIT * 100
        await bot.send_message(
            ADMIN_ID,
            f"✅ GRID BOT ONLINE\n"
            f"Equity: {equity:.2f}$ | ROI: {roi:.2f}%\n"
            f"Max DD: {MAX_DRAWDOWN:.2f}%\n"
            f"Deals: {DEALS}"
        )
        save_state()
        await asyncio.sleep(HEARTBEAT_INTERVAL)

# ================== COMMANDS ==================
@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id == ADMIN_ID:
        await msg.answer("🤖 GRID BOT RUNNING")

# ================== MAIN ==================
async def main():
    load_state()
    asyncio.create_task(grid_engine())
    asyncio.create_task(heartbeat())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())