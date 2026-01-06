import os
import asyncio
import aiohttp
import time
from statistics import mean
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ================= CONFIG =================
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

BINANCE_URL = "https://api.binance.com/api/v3/klines"
PRICE_URL = "https://api.binance.com/api/v3/ticker/price"

PAIRS = [
    "HUSDT", "SOLUSDT", "ETHUSDT", "RIVERUSDT", "LIGHTUSDT",
    "BEATUSDT", "CYSUSDT", "ZPKUSDT", "RAVEUSDT", "DOGEUSDT"
]

TIMEFRAMES = ["5m", "15m"]
CURRENT_TF = "5m"

ENABLED_PAIRS = {p: False for p in PAIRS}

SCAN_INTERVAL = 30
START_TS = time.time()

# ===== DRY RUN PARAMS =====
DRY_DEPOSIT = 100.0
DRY_LEVERAGE = 10

MAX_GRIDS = 2
MAX_DRAWDOWN_PCT = -15
DAILY_LOSS_LIMIT_PCT = -5
MAX_IDLE_MIN = 20
FLAT_KILL_DEALS = 8

BOT_MODE = "ACTIVE"  # ACTIVE / PAUSE / STOP

ACTIVE_GRIDS = {}

STATS = {
    "total_pnl": 0.0,
    "daily_pnl": 0.0,
    "deals": 0,
    "day": datetime.utcnow().date()
}

# ================= UTILS =================
def ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    e = sum(data[:period]) / period
    for p in data[period:]:
        e = p * k + e * (1 - k)
    return e

def atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        trs.append(tr)
    return mean(trs[-period:]) if len(trs) >= period else None

# ================= BINANCE =================
async def get_klines(symbol, interval, limit=120):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            BINANCE_URL,
            params={"symbol": symbol, "interval": interval, "limit": limit}
        ) as r:
            data = await r.json()
            return data if isinstance(data, list) else []

async def get_price(symbol):
    async with aiohttp.ClientSession() as s:
        async with s.get(PRICE_URL, params={"symbol": symbol}) as r:
            data = await r.json()
            return float(data["price"])

# ================= GRID LOGIC =================
async def analyze_for_grid(pair):
    kl = await get_klines(pair, CURRENT_TF)
    if len(kl) < 50:
        return None

    closes, highs, lows = [], [], []
    for k in kl:
        closes.append(float(k[4]))
        highs.append(float(k[2]))
        lows.append(float(k[3]))

    price = closes[-1]
    ema25 = ema(closes, 25)
    ema99 = ema(closes, 99)
    atr_val = atr(highs, lows, closes)

    if not all([ema25, ema99, atr_val]):
        return None

    atr_pct = atr_val / price * 100
    if atr_pct > 0.8:
        return None  # не флет

    direction = None
    if price > ema25 > ema99:
        direction = "LONG"
    elif price < ema25 < ema99:
        direction = "SHORT"

    if not direction:
        return None

    low = price * (1 - atr_pct * 2 / 100)
    high = price * (1 + atr_pct * 2 / 100)

    levels = 8
    step = (high - low) / levels
    size = (DRY_DEPOSIT / levels) * DRY_LEVERAGE

    orders = [{"price": low + step * i, "filled": False} for i in range(levels)]

    return {
        "pair": pair,
        "dir": direction,
        "low": low,
        "high": high,
        "orders": orders,
        "size": size,
        "last_trade": time.time(),
        "deals": 0,
        "pnl": 0.0
    }

# ================= ENGINE =================
async def process_grids():
    global BOT_MODE

    while True:
        today = datetime.utcnow().date()
        if today != STATS["day"]:
            STATS["day"] = today
            STATS["daily_pnl"] = 0

        # ----- GLOBAL RISK -----
        drawdown = STATS["total_pnl"] / DRY_DEPOSIT * 100
        daily_dd = STATS["daily_pnl"] / DRY_DEPOSIT * 100

        if drawdown <= MAX_DRAWDOWN_PCT:
            BOT_MODE = "STOP"
            ACTIVE_GRIDS.clear()
            await bot.send_message(ADMIN_ID, "🛑 MAX DRAWDOWN. BOT STOPPED")

        if daily_dd <= DAILY_LOSS_LIMIT_PCT:
            BOT_MODE = "PAUSE"
            ACTIVE_GRIDS.clear()
            await bot.send_message(ADMIN_ID, "⏸ DAILY LOSS LIMIT. PAUSE")

        if BOT_MODE != "ACTIVE":
            await asyncio.sleep(10)
            continue

        for p, g in list(ACTIVE_GRIDS.items()):
            price = await get_price(p)

            if (time.time() - g["last_trade"]) / 60 > MAX_IDLE_MIN:
                del ACTIVE_GRIDS[p]
                continue

            for o in g["orders"]:
                if not o["filled"]:
                    if g["dir"] == "LONG" and price <= o["price"]:
                        o["filled"] = True
                        g["last_trade"] = time.time()
                    elif g["dir"] == "SHORT" and price >= o["price"]:
                        o["filled"] = True
                        g["last_trade"] = time.time()
                else:
                    if g["dir"] == "LONG" and price >= o["price"] * 1.002:
                        pnl = (price - o["price"]) * g["size"]
                    elif g["dir"] == "SHORT" and price <= o["price"] * 0.998:
                        pnl = (o["price"] - price) * g["size"]
                    else:
                        continue

                    g["pnl"] += pnl
                    g["deals"] += 1
                    STATS["total_pnl"] += pnl
                    STATS["daily_pnl"] += pnl
                    STATS["deals"] += 1
                    o["filled"] = False
                    g["last_trade"] = time.time()

            if g["deals"] >= FLAT_KILL_DEALS and g["pnl"] <= 0:
                del ACTIVE_GRIDS[p]
                await bot.send_message(ADMIN_ID, f"⚠️ GRID CLOSED (FLAT)\n{p}")

        await asyncio.sleep(5)

# ================= SCANNER =================
async def scanner():
    while True:
        if BOT_MODE != "ACTIVE":
            await asyncio.sleep(30)
            continue

        if len(ACTIVE_GRIDS) >= MAX_GRIDS:
            await asyncio.sleep(30)
            continue

        for p, on in ENABLED_PAIRS.items():
            if not on or p in ACTIVE_GRIDS:
                continue

            grid = await analyze_for_grid(p)
            if not grid:
                continue

            ACTIVE_GRIDS[p] = grid
            await bot.send_message(
                ADMIN_ID,
                f"🧱 GRID START\n{p} ({CURRENT_TF})\nТип: {grid['dir']}"
            )

        await asyncio.sleep(SCAN_INTERVAL)

# ================= UI =================
def main_keyboard():
    rows = []
    for p, on in ENABLED_PAIRS.items():
        rows.append([
            InlineKeyboardButton(
                text=("🟢 " if on else "🔴 ") + p.replace("USDT", ""),
                callback_data=f"pair:{p}"
            )
        ])
    rows.append([
        InlineKeyboardButton(text=f"⏱ {CURRENT_TF}", callback_data="tf"),
        InlineKeyboardButton(text="📊 Статус", callback_data="status")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("🧱 GRID BOT v5 (SAFE)", reply_markup=main_keyboard())

@dp.callback_query()
async def callbacks(c: types.CallbackQuery):
    global CURRENT_TF
    if c.from_user.id != ADMIN_ID:
        return

    if c.data.startswith("pair:"):
        p = c.data.split(":")[1]
        ENABLED_PAIRS[p] = not ENABLED_PAIRS[p]

    elif c.data == "tf":
        i = TIMEFRAMES.index(CURRENT_TF)
        CURRENT_TF = TIMEFRAMES[(i + 1) % len(TIMEFRAMES)]

    elif c.data == "status":
        await c.message.answer(
            f"📊 STATUS\n\n"
            f"🧠 Mode: {BOT_MODE}\n"
            f"🧱 Grids: {', '.join(ACTIVE_GRIDS.keys()) or 'нет'}\n"
            f"📦 Deals: {STATS['deals']}\n"
            f"💰 Total PnL: {STATS['total_pnl']:.2f}$\n"
            f"📉 Daily: {STATS['daily_pnl']:.2f}$\n"
            f"(DRY {DRY_DEPOSIT}$ x{DRY_LEVERAGE})"
        )

    await c.message.edit_reply_markup(reply_markup=main_keyboard())
    await c.answer()

# ================= MAIN =================
async def main():
    asyncio.create_task(scanner())
    asyncio.create_task(process_grids())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())