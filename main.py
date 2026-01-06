import os
import asyncio
import aiohttp
import time
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# ================== CONFIG ==================
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

SYMBOL = "SOLUSDT"
TIMEFRAME = "5m"

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

# --- DRY RUN ---
DEPOSIT = 100.0
LEVERAGE = 10

MAKER_FEE = 0.0002
TAKER_FEE = 0.0004

MAX_MARGIN_PER_GRID = 0.10     # 10% депо
GRID_LEVELS = 10
GRID_SPREAD = 0.015            # 1.5%

DAILY_PROFIT_CAP = 0.05        # +5%
DAILY_LOSS_CAP = -0.03         # -3%

SCAN_INTERVAL = 30
HEARTBEAT_INTERVAL = 1800      # 30 минут

# ================== STATE ==================
bot = Bot(token=TOKEN)
dp = Dispatcher()

START_TS = time.time()

MODE = "ACTIVE"   # ACTIVE / PAUSE
GRID_ACTIVE = False
GRID_SIDE = None  # LONG / SHORT

ENTRY_PRICE = None
GRID_ORDERS = []

TOTAL_PNL = 0.0
DAILY_PNL = 0.0
TOTAL_DEALS = 0

# ================== UTILS ==================
def ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    e = sum(data[:period]) / period
    for p in data[period:]:
        e = p * k + e * (1 - k)
    return e

def calc_pnl(entry, exit, qty):
    notional = qty * exit
    margin = notional / LEVERAGE

    raw = (exit - entry) * qty if GRID_SIDE == "LONG" else (entry - exit) * qty
    fees = notional * (MAKER_FEE * 2)

    pnl = raw - fees
    roi = pnl / margin if margin > 0 else 0
    return pnl, roi

# ================== BINANCE ==================
async def get_klines(limit=120):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            BINANCE_KLINES,
            params={
                "symbol": SYMBOL,
                "interval": TIMEFRAME,
                "limit": limit
            }
        ) as r:
            return await r.json()

# ================== ANALYSIS ==================
async def detect_direction():
    kl = await get_klines()
    closes = [float(k[4]) for k in kl]

    ema7 = ema(closes, 7)
    ema25 = ema(closes, 25)

    if not ema7 or not ema25:
        return None

    if ema7 > ema25:
        return "LONG"
    if ema7 < ema25:
        return "SHORT"
    return None

# ================== GRID ==================
def build_grid(price, side):
    orders = []

    margin = DEPOSIT * MAX_MARGIN_PER_GRID
    notional = margin * LEVERAGE
    qty = notional / price / GRID_LEVELS

    for i in range(1, GRID_LEVELS + 1):
        step = GRID_SPREAD * i

        if side == "LONG":
            buy = price * (1 - step)
            sell = price * (1 + step)
        else:
            buy = price * (1 + step)
            sell = price * (1 - step)

        orders.append({
            "buy": buy,
            "sell": sell,
            "qty": qty,
            "filled": False
        })

    return orders

# ================== ENGINE ==================
async def grid_engine():
    global GRID_ACTIVE, GRID_SIDE, ENTRY_PRICE
    global TOTAL_PNL, DAILY_PNL, TOTAL_DEALS, MODE, GRID_ORDERS

    while True:
        if MODE != "ACTIVE":
            await asyncio.sleep(SCAN_INTERVAL)
            continue

        kl = await get_klines(limit=2)
        price = float(kl[-1][4])

        if not GRID_ACTIVE:
            side = await detect_direction()
            if not side:
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            GRID_SIDE = side
            ENTRY_PRICE = price
            GRID_ORDERS = build_grid(price, side)
            GRID_ACTIVE = True

            await bot.send_message(
                ADMIN_ID,
                f"🧠 GRID STARTED\n"
                f"Symbol: {SYMBOL}\n"
                f"Side: {side}\n"
                f"Entry: {price:.4f}\n"
                f"(DRY {DEPOSIT}$ x{LEVERAGE})"
            )

        for o in GRID_ORDERS:
            if o["filled"]:
                continue

            if GRID_SIDE == "LONG" and price <= o["buy"]:
                o["filled"] = True

            elif GRID_SIDE == "SHORT" and price >= o["buy"]:
                o["filled"] = True

            elif o["filled"]:
                pnl, roi = calc_pnl(ENTRY_PRICE, price, o["qty"])
                TOTAL_PNL += pnl
                DAILY_PNL += pnl
                TOTAL_DEALS += 1

        daily_roi = DAILY_PNL / DEPOSIT
        if daily_roi >= DAILY_PROFIT_CAP or daily_roi <= DAILY_LOSS_CAP:
            MODE = "PAUSE"
            GRID_ACTIVE = False
            await bot.send_message(
                ADMIN_ID,
                "🧯 GRID PAUSED\n"
                f"Daily PnL: {DAILY_PNL:.2f}$"
            )

        await asyncio.sleep(SCAN_INTERVAL)

# ================== HEARTBEAT ==================
async def heartbeat():
    while True:
        uptime = int((time.time() - START_TS) / 60)

        text = (
            "✅ GRID BOT ONLINE\n\n"
            f"Mode: {MODE}\n"
            f"Symbol: {SYMBOL}\n"
            f"TF: {TIMEFRAME}\n"
            f"Grid: {'ON' if GRID_ACTIVE else 'OFF'}\n"
            f"Deals: {TOTAL_DEALS}\n"
            f"Total PnL: {TOTAL_PNL:.2f}$\n"
            f"Daily PnL: {DAILY_PNL:.2f}$\n"
            f"Uptime: {uptime} min\n"
            f"(DRY {DEPOSIT}$ x{LEVERAGE})"
        )

        try:
            await bot.send_message(ADMIN_ID, text)
        except Exception:
            pass

        await asyncio.sleep(HEARTBEAT_INTERVAL)

# ================== COMMANDS ==================
@dp.message(Command("status"))
async def status(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    await msg.answer(
        f"📊 STATUS\n"
        f"Mode: {MODE}\n"
        f"Grid: {'ON' if GRID_ACTIVE else 'OFF'}\n"
        f"Deals: {TOTAL_DEALS}\n"
        f"Total PnL: {TOTAL_PNL:.2f}$\n"
        f"Daily PnL: {DAILY_PNL:.2f}$"
    )

# ================== MAIN ==================
async def main():
    asyncio.create_task(grid_engine())
    asyncio.create_task(heartbeat())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())