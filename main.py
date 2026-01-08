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

# ---- GRID PARAMS ----
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
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        ))
    return mean(tr[-ATR_PERIOD:]) if len(tr) >= ATR_PERIOD else None

def atr_profile(price, atr_val):
    pct = atr_val / price * 100
    if pct < 0.4:
        return 10, 3.5
    elif pct < 0.8:
        return 8, 2.5
    else:
        return 6, 1.8

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
            data = await r.json()
            return data if isinstance(data, list) else []

# ================== ANALYSIS ==================
async def analyze_pair(pair):
    kl = await get_klines(pair)
    if len(kl) < 50:
        LAST_REJECT_REASON[pair] = "not enough candles"
        return None

    closes = [float(k[4]) for k in kl]
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]

    price = closes[-1]
    e7 = ema(closes, 7)
    e25 = ema(closes, 25)
    atr_val = atr(highs, lows, closes)

    if not atr_val:
        LAST_REJECT_REASON[pair] = "ATR unavailable"
        return None

    if price > e7 > e25:
        side = "LONG"
    elif price < e7 < e25:
        side = "SHORT"
    else:
        LAST_REJECT_REASON[pair] = "EMA flat"
        return None

    return {"price": price, "side": side, "atr": atr_val}

# ================== GRID BUILD ==================
def build_grid(price, atr_val, side):
    levels, mult = atr_profile(price, atr_val)
    rng = atr_val * mult

    low, high = (
        (price - rng, price + rng)
        if side == "LONG"
        else (price + rng, price - rng)
    )

    step = abs(high - low) / levels
    margin = DEPOSIT * MAX_MARGIN_PER_GRID
    notional = margin * LEVERAGE
    qty = (notional / price) / levels

    orders = []

    for i in range(levels):
        entry = low + step * i if side == "LONG" else low - step * i
        exit = entry + step if side == "LONG" else entry - step

        exp = abs(exit - entry) * qty
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
        LAST_REJECT_REASON["grid"] = "orders filtered"
        return None

    return {
        "side": side,
        "low": min(low, high),
        "high": max(low, high),
        "orders": orders,
        "atr": atr_val,
        "levels": levels,
        "qty": qty,
        "created_at": time.time()
    }

# ================== ENGINE ==================
async def grid_engine():
    global TOTAL_PNL, DEALS, WIN_TRADES, LOSS_TRADES
    global GROSS_PROFIT, GROSS_LOSS, MAX_EQUITY, MAX_DRAWDOWN

    while True:
        # update active grids
        for pair, g in list(ACTIVE_GRIDS.items()):
            kl = await get_klines(pair, limit=2)
            if not kl:
                continue

            price = float(kl[-1][4])

            if pair not in ACTIVE_PAIRS or not (g["low"] <= price <= g["high"]):
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

                        if pnl > 0:
                            WIN_TRADES += 1
                            GROSS_PROFIT += pnl
                        else:
                            LOSS_TRADES += 1
                            GROSS_LOSS += pnl

                        equity = DEPOSIT + TOTAL_PNL
                        MAX_EQUITY = max(MAX_EQUITY, equity)
                        MAX_DRAWDOWN = min(
                            MAX_DRAWDOWN,
                            (equity - MAX_EQUITY) / MAX_EQUITY * 100
                        )

                        PAIR_STATS.setdefault(pair, {"pnl": 0.0, "deals": 0})
                        PAIR_STATS[pair]["pnl"] += pnl
                        PAIR_STATS[pair]["deals"] += 1

                        o["open"] = False
                        save_state()

        # start new grids
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

@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    uptime = int((time.time() - START_TS) / 60)
    equity = DEPOSIT + TOTAL_PNL
    roi = (equity - DEPOSIT) / DEPOSIT * 100
    pf = abs(GROSS_PROFIT / GROSS_LOSS) if GROSS_LOSS != 0 else float("inf")
    avg = TOTAL_PNL / DEALS if DEALS else 0
    wr = (WIN_TRADES / DEALS * 100) if DEALS else 0

    lines = [
        "📊 GRID BOT STATUS",
        "",
        f"Uptime: {uptime} min",
        f"Equity: {equity:.2f}$ | ROI: {roi:.2f}%",
        f"Deals: {DEALS} | WinRate: {wr:.1f}%",
        f"Avg PnL: {avg:.3f}$ | PF: {pf:.2f}",
        f"Max DD: {MAX_DRAWDOWN:.2f}%",
        "",
        f"Active grids: {len(ACTIVE_GRIDS)}/{MAX_GRIDS}"
    ]

    if ACTIVE_GRIDS:
        lines.append("")
        lines.append("Grids:")
        for p, g in ACTIVE_GRIDS.items():
            open_o = sum(1 for o in g["orders"] if o["open"])
            lines.append(
                f"{p} | {g['side']} | ATR {g['atr']:.4f} | "
                f"{open_o}/{len(g['orders'])} orders"
            )

    lines.append("")
    lines.append("Active pairs:")
    lines.append(", ".join(ACTIVE_PAIRS))

    await msg.answer("\n".join(lines))

@dp.message(Command("why"))
async def cmd_why(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    lines = ["🤔 WHY NO GRID", ""]
    for p in ACTIVE_PAIRS:
        if p in ACTIVE_GRIDS:
            lines.append(f"{p}: grid active")
        else:
            lines.append(f"{p}: {LAST_REJECT_REASON.get(p, 'waiting')}")

    await msg.answer("\n".join(lines))

# ================== HEARTBEAT ==================
async def heartbeat():
    while True:
        uptime = int((time.time() - START_TS) / 60)
        await bot.send_message(
            ADMIN_ID,
            f"📡 GRID BOT HEARTBEAT\n"
            f"Uptime: {uptime} min\n"
            f"Active grids: {len(ACTIVE_GRIDS)}/{MAX_GRIDS}"
        )
        save_state()
        await asyncio.sleep(HEARTBEAT_INTERVAL)

# ================== MAIN ==================
async def main():
    load_state()
    asyncio.create_task(grid_engine())
    asyncio.create_task(heartbeat())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())