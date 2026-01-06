import os
import asyncio
import aiohttp
import time
from statistics import mean
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ================== CONFIG ==================
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

BINANCE_URL = "https://api.binance.com/api/v3/klines"

PAIRS = [
    "HUSDT", "SOLUSDT", "ETHUSDT", "RIVERUSDT", "LIGHTUSDT",
    "BEATUSDT", "CYSUSDT", "ZPKUSDT", "RAVEUSDT", "DOGEUSDT"
]

TIMEFRAMES = ["5m", "15m"]
CURRENT_TF = "5m"

ENABLED_PAIRS = {p: False for p in PAIRS}

SCAN_INTERVAL = 60
START_TS = time.time()

# DRY RUN PARAMS
DRY_DEPOSIT = 100.0
DRY_LEVERAGE = 10

LAST_DECISION = {}

# ================== UTILS ==================
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

# ================== BINANCE ==================
async def get_klines(symbol, interval, limit=120):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            BINANCE_URL,
            params={"symbol": symbol, "interval": interval, "limit": limit}
        ) as r:
            data = await r.json()
            return data if isinstance(data, list) else []

# ================== GRID DECISION ==================
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

    # === FLAT FILTER ===
    is_flat = atr_pct < 0.6

    # === DIRECTION ===
    direction = "NEUTRAL"
    if price > ema25 > ema99:
        direction = "LONG"
    elif price < ema25 < ema99:
        direction = "SHORT"

    # === DECISION ===
    decision = "WAIT"
    if is_flat:
        decision = "START GRID"

    return {
        "price": price,
        "atr_pct": atr_pct,
        "flat": is_flat,
        "direction": direction,
        "decision": decision
    }

# ================== KEYBOARD ==================
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

# ================== HANDLERS ==================
@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("🧱 GRID BOT v3 (DRY-RUN)", reply_markup=main_keyboard())

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
        uptime = int((time.time() - START_TS) / 60)
        enabled = [p for p, v in ENABLED_PAIRS.items() if v]
        await c.message.answer(
            f"📊 GRID BOT STATUS\n\n"
            f"🕒 Аптайм: {uptime} мин\n"
            f"⏱ TF: {CURRENT_TF}\n"
            f"📈 Пары: {', '.join(enabled) if enabled else 'нет'}\n\n"
            f"(DRY-RUN: депо {DRY_DEPOSIT}$, плечо x{DRY_LEVERAGE})"
        )

    await c.message.edit_reply_markup(reply_markup=main_keyboard())
    await c.answer()

# ================== SCANNER ==================
async def scanner():
    while True:
        for p, on in ENABLED_PAIRS.items():
            if not on:
                continue

            try:
                res = await analyze_for_grid(p)
                if not res:
                    continue

                key = f"{p}:{res['decision']}:{res['direction']}"
                if LAST_DECISION.get(p) == key:
                    continue

                LAST_DECISION[p] = key

                text = (
                    f"🧱 GRID ANALYSIS\n"
                    f"{p} ({CURRENT_TF})\n\n"
                    f"Цена: {res['price']:.4f}\n"
                    f"ATR: {res['atr_pct']:.2f}%\n"
                    f"Флэт: {'ДА' if res['flat'] else 'НЕТ'}\n"
                    f"Направление: {res['direction']}\n\n"
                    f"Решение: {res['decision']}\n\n"
                    f"(DRY-RUN)"
                )

                await bot.send_message(ADMIN_ID, text)

            except Exception as e:
                await bot.send_message(ADMIN_ID, f"❌ {p} ERROR: {e}")

        await asyncio.sleep(SCAN_INTERVAL)

# ================== MAIN ==================
async def main():
    asyncio.create_task(scanner())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())