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
    "BNBUSDT", "DOGEUSDT", "AVAXUSDT", "HUSDT", "CYSUSDT"
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

PAIR_STATS = {}
TOTAL_PNL = 0.0
DEALS = 0

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
            "MAX_EQUITY": MAX_EQUITY,
            "MAX_DRAWDOWN": MAX_DRAWDOWN
        }, f)

def load_state():
    global ACTIVE_PAIRS, ACTIVE_GRIDS, PAIR_STATS
    global TOTAL_PNL, DEALS, MAX_EQUITY, MAX_DRAWDOWN

    if not os.path.exists(STATE_FILE):
        return

    with open(STATE_FILE) as f:
        d = json.load(f)

    ACTIVE_PAIRS = d.get("ACTIVE_PAIRS", ACTIVE_PAIRS)
    ACTIVE_GRIDS = d.get("ACTIVE_GRIDS", {})
    PAIR_STATS = d.get("PAIR_STATS", {})
    TOTAL_PNL = d.get("TOTAL_PNL", 0.0)
    DEALS = d.get("DEALS", 0)
    MAX_EQUITY = d.get("MAX_EQUITY", DEPOSIT)
    MAX_DRAWDOWN = d.get("MAX_DRAWDOWN", 0.0)

# ================== TRADE LOG ==================
def log_trade(pair, side, entry, exit, qty, pnl):
    equity = DEPOSIT + TOTAL_PNL
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc","pair","side","entry","exit","qty","pnl","equity"])
        w.writerow([
            datetime.utcnow().isoformat(),
            pair, side,
            round(entry,6), round(exit,6),
            round(qty,6),
            round(pnl,4),
            round(equity,4)
        ])

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

def atr_mult(price, atr):
    pct = atr / price * 100
    if pct < 0.4: return 3.5
    if pct < 0.8: return 2.5
    return 1.8

def grid_levels(price, atr):
    pct = atr / price * 100
    if pct < 0.4: return 10
    if pct < 0.8: return 8
    return 6

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
        return None

    c = [float(k[4]) for k in kl]
    h = [float(k[2]) for k in kl]
    l = [float(k[3]) for k in kl]

    price = c[-1]
    e7 = ema(c,7)
    e25 = ema(c,25)
    a = atr(h,l,c)

    if not a:
        return None

    if price > e7 > e25:
        side = "LONG"
    elif price < e7 < e25:
        side = "SHORT"
    else:
        return None

    return {"price":price,"side":side,"atr":a}

# ================== GRID ==================
def build_grid(price, atr_val, side):
    levels = grid_levels(price, atr_val)
    rng = atr_val * atr_mult(price, atr_val)

    if side == "LONG":
        low, high = price - rng, price + rng
    else:
        high, low = price + rng, price - rng

    step = (high - low) / levels

    margin = DEPOSIT * MAX_MARGIN_PER_GRID
    notional = margin * LEVERAGE
    qty = (notional / price) / levels

    orders = []

    for i in range(levels):
        if side == "LONG":
            entry = low + step * i
            exit = entry + step
            exp = (exit - entry) * qty
        else:
            entry = high - step * i
            exit = entry - step
            exp = (entry - exit) * qty

        fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)

        if entry * qty < MIN_ORDER_NOTIONAL:
            continue
        if exp - fees < MIN_EXPECTED_PNL:
            continue

        orders.append({
            "entry": entry,
            "exit": exit,
            "qty": qty,
            "open": False
        })

    if len(orders) < 3:
        return None

    return {
        "side": side,
        "low": low,
        "high": high,
        "orders": orders
    }

def calc_pnl(entry, exit, qty, side):
    if side == "LONG":
        gross = (exit - entry) * qty
    else:
        gross = (entry - exit) * qty
    fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)
    return gross - fees

# ================== ENGINE ==================
async def grid_engine():
    global TOTAL_PNL, DEALS, MAX_EQUITY, MAX_DRAWDOWN

    while True:
        # ---- UPDATE GRIDS ----
        for pair, g in list(ACTIVE_GRIDS.items()):
            kl = await get_klines(pair, limit=2)
            price = float(kl[-1][4])

            if pair not in ACTIVE_PAIRS:
                del ACTIVE_GRIDS[pair]
                continue

            if price < g["low"] or price > g["high"]:
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

                        equity = DEPOSIT + TOTAL_PNL
                        MAX_EQUITY = max(MAX_EQUITY, equity)
                        dd = (equity - MAX_EQUITY) / MAX_EQUITY * 100
                        MAX_DRAWDOWN = min(MAX_DRAWDOWN, dd)

                        PAIR_STATS.setdefault(pair, {"pnl":0,"deals":0})
                        PAIR_STATS[pair]["pnl"] += pnl
                        PAIR_STATS[pair]["deals"] += 1

                        log_trade(pair, g["side"], o["entry"], o["exit"], o["qty"], pnl)
                        o["open"] = False
                        save_state()

        # ---- START NEW GRIDS ----
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
        await bot.send_message(
            ADMIN_ID,
            f"✅ GRID BOT ONLINE\n"
            f"Active pairs: {', '.join(ACTIVE_PAIRS)}\n"
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

@dp.message(Command("pairs"))
async def show_pairs(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(
        "📊 Active pairs:\n" + "\n".join(f"• {p}" for p in ACTIVE_PAIRS)
    )

@dp.message(Command("pair"))
async def manage_pair(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    parts = msg.text.split()
    if len(parts) != 3:
        await msg.answer("Используй: /pair add|remove SYMBOL")
        return

    action, pair = parts[1], parts[2].upper()

    if pair not in ALL_PAIRS:
        await msg.answer("❌ Пара не разрешена")
        return

    if action == "add":
        if pair not in ACTIVE_PAIRS:
            ACTIVE_PAIRS.append(pair)
            save_state()
            await msg.answer(f"✅ {pair} добавлена")
        else:
            await msg.answer("Уже активна")

    elif action == "remove":
        if pair in ACTIVE_PAIRS:
            ACTIVE_PAIRS.remove(pair)
            ACTIVE_GRIDS.pop(pair, None)
            save_state()
            await msg.answer(f"🛑 {pair} удалена")
        else:
            await msg.answer("Пара не активна")

    else:
        await msg.answer("Команда: /pair add|remove SYMBOL")

# ================== MAIN ==================
async def main():
    load_state()
    asyncio.create_task(grid_engine())
    asyncio.create_task(heartbeat())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())