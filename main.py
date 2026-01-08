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

TIMEFRAME = "15m"
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

ACTIVE_PAIRS = ["SOLUSDT", "BNBUSDT"]
ACTIVE_GRIDS = {}
LAST_REJECT_REASON = {}

TOTAL_PNL = 0.0
DEALS = 0

# ================== STATE IO ==================
def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "ACTIVE_PAIRS": ACTIVE_PAIRS,
            "ACTIVE_GRIDS": ACTIVE_GRIDS,
            "TOTAL_PNL": TOTAL_PNL,
            "DEALS": DEALS
        }, f)

def load_state():
    global ACTIVE_PAIRS, ACTIVE_GRIDS, TOTAL_PNL, DEALS
    if not os.path.exists(STATE_FILE):
        return
    with open(STATE_FILE) as f:
        d = json.load(f)
    ACTIVE_PAIRS = d.get("ACTIVE_PAIRS", ACTIVE_PAIRS)
    ACTIVE_GRIDS = d.get("ACTIVE_GRIDS", {})
    TOTAL_PNL = d.get("TOTAL_PNL", 0.0)
    DEALS = d.get("DEALS", 0)

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

    # === ВАЖНО: FLAT ТЕПЕРЬ РАЗРЕШЕН ===
    if price > e7 > e25:
        side = "LONG"
    elif price < e7 < e25:
        side = "SHORT"
    else:
        side = "FLAT"

    return {"price": price, "side": side, "atr": a}

# ================== GRID ==================
def build_grid(price, atr_val, side):
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
        if side == "LONG":
            entry = low + step * i
            exit = entry + step
        elif side == "SHORT":
            entry = high - step * i
            exit = entry - step
        else:  # FLAT
            entry = low + step * i
            exit = entry + step

        if entry * qty < MIN_ORDER_NOTIONAL:
            continue

        orders.append({
            "entry": entry,
            "exit": exit,
            "qty": qty,
            "open": False
        })

    if len(orders) < 3:
        LAST_REJECT_REASON["grid"] = "orders filtered"
        return None

    return {
        "side": side,
        "low": low,
        "high": high,
        "orders": orders,
        "atr": atr_val
    }

def calc_pnl(entry, exit, qty, side):
    gross = (exit - entry) * qty
    fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)
    return gross - fees

# ================== ENGINE ==================
async def grid_engine():
    global TOTAL_PNL, DEALS

    while True:
        # --- update grids ---
        for pair, g in list(ACTIVE_GRIDS.items()):
            kl = await get_klines(pair, limit=2)
            if not kl:
                continue

            price = float(kl[-1][4])

            if pair not in ACTIVE_PAIRS or not (g["low"] <= price <= g["high"]):
                del ACTIVE_GRIDS[pair]
                continue

            for o in g["orders"]:
                if not o["open"] and price <= o["entry"]:
                    o["open"] = True
                elif o["open"] and price >= o["exit"]:
                    pnl = calc_pnl(o["entry"], o["exit"], o["qty"], g["side"])
                    TOTAL_PNL += pnl
                    DEALS += 1
                    o["open"] = False
                    save_state()

        # --- start new grids ---
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

    if action == "add" and pair not in ACTIVE_PAIRS:
        ACTIVE_PAIRS.append(pair)
        save_state()
        await msg.answer(f"✅ {pair} added")

    elif action == "remove" and pair in ACTIVE_PAIRS:
        ACTIVE_PAIRS.remove(pair)
        ACTIVE_GRIDS.pop(pair, None)
        save_state()
        await msg.answer(f"🛑 {pair} removed")

@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    uptime = int((time.time() - START_TS) / 60)
    equity = DEPOSIT + TOTAL_PNL
    roi = (equity - DEPOSIT) / DEPOSIT * 100

    lines = [
        "📊 GRID BOT STATUS",
        f"Uptime: {uptime} min",
        f"Equity: {equity:.2f}$ | ROI: {roi:.2f}%",
        f"Deals: {DEALS}",
        f"Active grids: {len(ACTIVE_GRIDS)}/{MAX_GRIDS}",
        "",
        "Grids:"
    ]

    for p, g in ACTIVE_GRIDS.items():
        lines.append(
            f"• {p} | {g['side']} | ATR {g['atr']:.6f}\n"
            f"  {g['low']:.6f} → {g['high']:.6f}"
        )

    await msg.answer("\n".join(lines))

@dp.message(Command("why"))
async def cmd_why(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    lines = ["🤔 WHY NO GRID"]
    for p in ACTIVE_PAIRS:
        lines.append(f"{p}: {LAST_REJECT_REASON.get(p, 'waiting')}")
    await msg.answer("\n".join(lines))

# ================== HEARTBEAT ==================
async def heartbeat():
    while True:
        await bot.send_message(
            ADMIN_ID,
            f"📡 GRID BOT ONLINE\n"
            f"Active pairs: {', '.join(ACTIVE_PAIRS)}\n"
            f"Active grids: {len(ACTIVE_GRIDS)}/{MAX_GRIDS}"
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