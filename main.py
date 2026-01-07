import os
import json
import asyncio
import aiohttp
import time
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
TIMEFRAME = "5m"
BINANCE_URL = "https://api.binance.com/api/v3/klines"

DEPOSIT = 100.0
GRID_LEVELS = 10          # как в Binance Grid
GRID_RANGE_PCT = 4.0      # ±2% от цены
SCAN_INTERVAL = 10
HEARTBEAT_INTERVAL = 1800

FEE = 0.0004  # условная комиссия

# ================== STATE ==================
START_TS = time.time()

ACTIVE_PAIRS = ["BTCUSDT"]
GRIDS = {}               # pair -> grid
LAST_REASON = {}         # pair -> reason

TOTAL_PNL = 0.0
DEALS = 0
WIN = 0
LOSS = 0

# ================== STATE IO ==================
def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "ACTIVE_PAIRS": ACTIVE_PAIRS,
            "GRIDS": GRIDS,
            "TOTAL_PNL": TOTAL_PNL,
            "DEALS": DEALS,
            "WIN": WIN,
            "LOSS": LOSS
        }, f)

def load_state():
    global ACTIVE_PAIRS, GRIDS, TOTAL_PNL, DEALS, WIN, LOSS
    if not os.path.exists(STATE_FILE):
        return
    with open(STATE_FILE) as f:
        d = json.load(f)
    ACTIVE_PAIRS = d.get("ACTIVE_PAIRS", ACTIVE_PAIRS)
    GRIDS = d.get("GRIDS", {})
    TOTAL_PNL = d.get("TOTAL_PNL", 0.0)
    DEALS = d.get("DEALS", 0)
    WIN = d.get("WIN", 0)
    LOSS = d.get("LOSS", 0)

# ================== BINANCE ==================
async def get_price(symbol):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            BINANCE_URL,
            params={"symbol": symbol, "interval": TIMEFRAME, "limit": 1}
        ) as r:
            d = await r.json()
            if not isinstance(d, list):
                return None
            return float(d[-1][4])

# ================== GRID ==================
def build_grid(price):
    half = GRID_RANGE_PCT / 2 / 100
    low = price * (1 - half)
    high = price * (1 + half)
    step = (high - low) / GRID_LEVELS
    qty = (DEPOSIT / GRID_LEVELS) / price

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

    return {
        "low": low,
        "high": high,
        "step": step,
        "orders": orders
    }

def calc_pnl(buy, sell, qty):
    gross = (sell - buy) * qty
    fee = (buy + sell) * qty * FEE
    return gross - fee

# ================== ENGINE ==================
async def grid_engine():
    global TOTAL_PNL, DEALS, WIN, LOSS

    while True:
        for pair in ACTIVE_PAIRS:
            price = await get_price(pair)
            if not price:
                LAST_REASON[pair] = "no price"
                continue

            if pair not in GRIDS:
                GRIDS[pair] = build_grid(price)
                LAST_REASON[pair] = "grid started"
                save_state()
                continue

            g = GRIDS[pair]

            if not (g["low"] <= price <= g["high"]):
                del GRIDS[pair]
                LAST_REASON[pair] = "price left range"
                save_state()
                continue

            for o in g["orders"]:
                if not o["open"] and price <= o["buy"]:
                    o["open"] = True
                elif o["open"] and price >= o["sell"]:
                    pnl = calc_pnl(o["buy"], o["sell"], o["qty"])
                    TOTAL_PNL += pnl
                    DEALS += 1
                    WIN += 1 if pnl > 0 else 0
                    LOSS += 1 if pnl <= 0 else 0
                    o["open"] = False
                    save_state()

        await asyncio.sleep(SCAN_INTERVAL)

# ================== COMMANDS ==================
@dp.message(Command("pairs"))
async def pairs(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("Active pairs:\n" + "\n".join(ACTIVE_PAIRS))

@dp.message(Command("pair"))
async def pair(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    p = msg.text.split()
    if len(p) != 3:
        await msg.answer("Usage: /pair add|remove SYMBOL")
        return

    action, sym = p[1], p[2].upper()

    if action == "add" and sym not in ACTIVE_PAIRS:
        ACTIVE_PAIRS.append(sym)
        save_state()
        await msg.answer(f"✅ {sym} added")

    elif action == "remove" and sym in ACTIVE_PAIRS:
        ACTIVE_PAIRS.remove(sym)
        GRIDS.pop(sym, None)
        save_state()
        await msg.answer(f"🛑 {sym} removed")

@dp.message(Command("stats"))
async def stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    equity = DEPOSIT + TOTAL_PNL
    uptime = int((time.time() - START_TS) / 60)

    lines = [
        "📊 BINANCE GRID — BASE",
        f"Uptime: {uptime} min",
        f"Equity: {equity:.2f}$",
        f"Deals: {DEALS} | Win: {WIN} | Loss: {LOSS}",
        "",
        f"Active grids: {len(GRIDS)}",
    ]

    for p, g in GRIDS.items():
        open_o = sum(1 for o in g["orders"] if o["open"])
        lines.append(
            f"{p}: {g['low']:.4f} → {g['high']:.4f} | "
            f"step {g['step']:.6f} | open {open_o}"
        )

    await msg.answer("\n".join(lines))

@dp.message(Command("why"))
async def why(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    lines = ["🤔 WHY", ""]
    for p in ACTIVE_PAIRS:
        lines.append(f"{p}: {LAST_REASON.get(p, 'waiting')}")
    await msg.answer("\n".join(lines))

# ================== HEARTBEAT ==================
async def heartbeat():
    while True:
        await bot.send_message(
            ADMIN_ID,
            f"📡 GRID BOT ONLINE | grids: {len(GRIDS)} | pairs: {len(ACTIVE_PAIRS)}"
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