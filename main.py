import os
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

# ================== SETTINGS ==================
PAIRS = ["SOLUSDT", "ETHUSDT", "BTCUSDT"]
TIMEFRAME = "5m"

BINANCE_URL = "https://api.binance.com/api/v3/klines"

# ---- DRY RUN ----
DEPOSIT = 100.0
LEVERAGE = 10
MAX_GRIDS = 2
MAX_MARGIN_PER_GRID = 0.10

MAKER_FEE = 0.0002
TAKER_FEE = 0.0004

ATR_PERIOD = 14
ATR_MULT = 2.5
GRID_LEVELS = 8

SCAN_INTERVAL = 20
HEARTBEAT_INTERVAL = 1800

# ---- RISK MANAGEMENT ----
DAILY_STOP_LOSS_PCT = -0.03
DAILY_TAKE_PROFIT_PCT = 0.05
WEEKLY_STOP_LOSS_PCT = -0.08
WEEKLY_TAKE_PROFIT_PCT = 0.12

# ================== STATE ==================
START_TS = time.time()

BOT_MODE = "ACTIVE"  # ACTIVE / PAUSE_DAY / PAUSE_WEEK

ACTIVE_GRIDS = {}

TOTAL_PNL = 0.0
DAILY_PNL = 0.0
WEEKLY_PNL = 0.0
DEALS = 0

LAST_DAY = datetime.utcnow().date()
LAST_WEEK = datetime.utcnow().isocalendar().week

BTC_CONTEXT = "⚪ BTC FLAT"

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

# ================== BINANCE ==================
async def get_klines(symbol, limit=120):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            BINANCE_URL,
            params={
                "symbol": symbol,
                "interval": TIMEFRAME,
                "limit": limit
            }
        ) as r:
            data = await r.json()
            return data if isinstance(data, list) else []

# ================== BTC CONTEXT ==================
async def update_btc_context():
    global BTC_CONTEXT

    kl = await get_klines("BTCUSDT", limit=100)
    if len(kl) < 50:
        return

    closes = [float(k[4]) for k in kl]
    price = closes[-1]

    ema7 = ema(closes, 7)
    ema25 = ema(closes, 25)

    if not ema7 or not ema25:
        return

    if price > ema7 > ema25:
        BTC_CONTEXT = "🟢 BTC BULL"
    elif price < ema7 < ema25:
        BTC_CONTEXT = "🔴 BTC BEAR"
    else:
        BTC_CONTEXT = "⚪ BTC FLAT"

# ================== ANALYSIS ==================
async def analyze_pair(pair):
    kl = await get_klines(pair)
    if len(kl) < 50:
        return None

    closes = [float(k[4]) for k in kl]
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]

    price = closes[-1]
    ema7 = ema(closes, 7)
    ema25 = ema(closes, 25)
    atr_val = atr(highs, lows, closes)

    if not ema7 or not ema25 or not atr_val:
        return None

    if price > ema7 > ema25:
        side = "LONG"
    elif price < ema7 < ema25:
        side = "SHORT"
    else:
        return None

    return {
        "price": price,
        "side": side,
        "atr": atr_val
    }

# ================== GRID ==================
def build_grid(price, atr_val):
    rng = atr_val * ATR_MULT
    low = price - rng
    high = price + rng

    step = (high - low) / GRID_LEVELS
    margin = DEPOSIT * MAX_MARGIN_PER_GRID
    notional = margin * LEVERAGE
    qty = (notional / price) / GRID_LEVELS

    orders = []
    for i in range(GRID_LEVELS):
        buy = low + step * i
        sell = buy + step
        orders.append({
            "buy": buy,
            "sell": sell,
            "qty": qty,
            "open": False
        })

    return low, high, orders

def calc_pnl(buy, sell, qty):
    gross = (sell - buy) * qty
    fee = (buy * qty * MAKER_FEE) + (sell * qty * TAKER_FEE)
    return gross - fee

# ================== RISK CHECK ==================
async def risk_check():
    global BOT_MODE

    daily_roi = DAILY_PNL / DEPOSIT
    weekly_roi = WEEKLY_PNL / DEPOSIT

    if BOT_MODE == "ACTIVE":
        if daily_roi <= DAILY_STOP_LOSS_PCT or daily_roi >= DAILY_TAKE_PROFIT_PCT:
            BOT_MODE = "PAUSE_DAY"
            ACTIVE_GRIDS.clear()
            await bot.send_message(
                ADMIN_ID,
                f"⏸ DAILY LIMIT HIT\nDaily PnL: {DAILY_PNL:.2f}$\n{BTC_CONTEXT}"
            )

        elif weekly_roi <= WEEKLY_STOP_LOSS_PCT or weekly_roi >= WEEKLY_TAKE_PROFIT_PCT:
            BOT_MODE = "PAUSE_WEEK"
            ACTIVE_GRIDS.clear()
            await bot.send_message(
                ADMIN_ID,
                f"⏸ WEEKLY LIMIT HIT\nWeekly PnL: {WEEKLY_PNL:.2f}$\n{BTC_CONTEXT}"
            )

# ================== ENGINE ==================
async def grid_engine():
    global TOTAL_PNL, DAILY_PNL, WEEKLY_PNL, DEALS
    global LAST_DAY, LAST_WEEK, BOT_MODE

    while True:
        await update_btc_context()

        now = datetime.utcnow()

        if now.date() != LAST_DAY:
            DAILY_PNL = 0.0
            LAST_DAY = now.date()
            if BOT_MODE == "PAUSE_DAY":
                BOT_MODE = "ACTIVE"

        week = now.isocalendar().week
        if week != LAST_WEEK:
            WEEKLY_PNL = 0.0
            LAST_WEEK = week
            if BOT_MODE == "PAUSE_WEEK":
                BOT_MODE = "ACTIVE"

        await risk_check()

        if BOT_MODE != "ACTIVE":
            await asyncio.sleep(SCAN_INTERVAL)
            continue

        for pair, g in list(ACTIVE_GRIDS.items()):
            kl = await get_klines(pair, limit=2)
            price = float(kl[-1][4])

            if price < g["low"] or price > g["high"]:
                del ACTIVE_GRIDS[pair]
                await bot.send_message(
                    ADMIN_ID,
                    f"🛑 GRID STOP (ATR)\n{pair}\n{BTC_CONTEXT}"
                )
                continue

            for o in g["orders"]:
                if not o["open"] and price <= o["buy"]:
                    o["open"] = True
                elif o["open"] and price >= o["sell"]:
                    pnl = calc_pnl(o["buy"], o["sell"], o["qty"])
                    TOTAL_PNL += pnl
                    DAILY_PNL += pnl
                    WEEKLY_PNL += pnl
                    DEALS += 1
                    o["open"] = False

        if len(ACTIVE_GRIDS) < MAX_GRIDS:
            for pair in PAIRS:
                if pair in ACTIVE_GRIDS:
                    continue

                res = await analyze_pair(pair)
                if not res:
                    continue

                low, high, orders = build_grid(res["price"], res["atr"])
                ACTIVE_GRIDS[pair] = {
                    "side": res["side"],
                    "low": low,
                    "high": high,
                    "orders": orders
                }

                await bot.send_message(
                    ADMIN_ID,
                    f"🧱 GRID START\n"
                    f"{pair}\n"
                    f"Side: {res['side']}\n"
                    f"{BTC_CONTEXT}"
                )

                if len(ACTIVE_GRIDS) >= MAX_GRIDS:
                    break

        await asyncio.sleep(SCAN_INTERVAL)

# ================== HEARTBEAT ==================
async def heartbeat():
    while True:
        uptime = int((time.time() - START_TS) / 60)
        await bot.send_message(
            ADMIN_ID,
            "✅ GRID BOT ONLINE\n\n"
            f"{BTC_CONTEXT}\n"
            f"Mode: {BOT_MODE}\n"
            f"Grids: {', '.join(ACTIVE_GRIDS) if ACTIVE_GRIDS else 'нет'}\n"
            f"Deals: {DEALS}\n"
            f"Total: {TOTAL_PNL:.2f}$\n"
            f"Daily: {DAILY_PNL:.2f}$\n"
            f"Weekly: {WEEKLY_PNL:.2f}$\n"
            f"Uptime: {uptime} min\n"
            f"(DRY {DEPOSIT}$ x{LEVERAGE})"
        )
        await asyncio.sleep(HEARTBEAT_INTERVAL)

# ================== COMMANDS ==================
@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("🤖 GRID BOT RUNNING\n" + BTC_CONTEXT)

# ================== MAIN ==================
async def main():
    asyncio.create_task(grid_engine())
    asyncio.create_task(heartbeat())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())