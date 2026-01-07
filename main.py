# ====== FULL FILE main.py (FIXED PAIRS CONTROL) ======

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

ATR_PERIOD = 14
SCAN_INTERVAL = 20

# ================== STATE ==================
ACTIVE_PAIRS = ["BTCUSDT", "ETHUSDT"]
ACTIVE_GRIDS = {}
LAST_REJECT_REASON = {}

START_TS = time.time()

# ================== STATE IO ==================
def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "ACTIVE_PAIRS": ACTIVE_PAIRS
        }, f)

def load_state():
    global ACTIVE_PAIRS
    if not os.path.exists(STATE_FILE):
        save_state()
        return
    with open(STATE_FILE) as f:
        d = json.load(f)
    ACTIVE_PAIRS[:] = d.get("ACTIVE_PAIRS", ACTIVE_PAIRS)

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
    e7 = ema(c,7)
    e25 = ema(c,25)
    a = atr(h,l,c)

    if not a:
        LAST_REJECT_REASON[pair] = "ATR_FAIL"
        return None

    if price > e7 > e25:
        mode = "TREND_LONG"
    elif price < e7 < e25:
        mode = "TREND_SHORT"
    else:
        mode = "RANGE"

    return {
        "price": price,
        "atr": a,
        "mode": mode
    }

# ================== ENGINE ==================
async def grid_engine():
    while True:
        # удаляем сетки по неактивным парам
        for pair in list(ACTIVE_GRIDS.keys()):
            if pair not in ACTIVE_PAIRS:
                ACTIVE_GRIDS.pop(pair, None)

        # старт новых сеток
        if len(ACTIVE_GRIDS) < MAX_GRIDS:
            for pair in ACTIVE_PAIRS:
                if pair in ACTIVE_GRIDS:
                    continue

                res = await analyze_pair(pair)
                if not res:
                    continue

                ACTIVE_GRIDS[pair] = {
                    "mode": res["mode"],
                    "started": datetime.utcnow().isoformat()
                }

                if len(ACTIVE_GRIDS) >= MAX_GRIDS:
                    break

        await asyncio.sleep(SCAN_INTERVAL)

# ================== COMMANDS ==================
@dp.message(Command("pairs"))
async def cmd_pairs(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(
        "📊 Active pairs:\n" + "\n".join(f"• {p}" for p in ACTIVE_PAIRS)
    )

@dp.message(Command("pair"))
async def cmd_pair(msg: types.Message):
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

@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    lines = ["📊 GRID BOT STATUS", ""]
    lines.append(f"Active grids: {len(ACTIVE_GRIDS)}/{MAX_GRIDS}")
    lines.append("")

    for p in ACTIVE_PAIRS:
        if p in ACTIVE_GRIDS:
            lines.append(f"{p}: ACTIVE | {ACTIVE_GRIDS[p]['mode']}")
        else:
            lines.append(f"{p}: NO GRID | {LAST_REJECT_REASON.get(p,'waiting')}")

    await msg.answer("\n".join(lines))

# ================== MAIN ==================
async def main():
    load_state()
    asyncio.create_task(grid_engine())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())