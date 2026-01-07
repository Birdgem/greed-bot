# ====== FULL FILE main.py ======

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
    "BNBUSDT", "DOGEUSDT", "AVAXUSDT",
    "HUSDT", "CYSUSDT"
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
LAST_REJECT_REASON = {}

STATS = {
    "equity": DEPOSIT,
    "total_pnl": 0.0,
    "deals": 0,
    "wins": 0,
    "losses": 0,
    "gross_profit": 0.0,
    "gross_loss": 0.0,
    "grids_started": 0,
    "grids_rejected": 0,
    "orders_total": 0,
    "orders_filtered": 0,
    "max_equity": DEPOSIT,
    "max_dd": 0.0
}

PAIR_STATS = {}

# ================== STATE IO ==================
def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "ACTIVE_PAIRS": ACTIVE_PAIRS,
            "ACTIVE_GRIDS": ACTIVE_GRIDS,
            "STATS": STATS,
            "PAIR_STATS": PAIR_STATS,
            "LAST_REJECT_REASON": LAST_REJECT_REASON
        }, f)

def load_state():
    global ACTIVE_PAIRS, ACTIVE_GRIDS, STATS, PAIR_STATS, LAST_REJECT_REASON
    if not os.path.exists(STATE_FILE):
        return
    with open(STATE_FILE) as f:
        d = json.load(f)
    ACTIVE_PAIRS = d.get("ACTIVE_PAIRS", ACTIVE_PAIRS)
    ACTIVE_GRIDS = d.get("ACTIVE_GRIDS", {})
    STATS.update(d.get("STATS", {}))
    PAIR_STATS = d.get("PAIR_STATS", {})
    LAST_REJECT_REASON = d.get("LAST_REJECT_REASON", {})

# ================== BINANCE ==================
async def get_klines(symbol, limit=120):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            BINANCE_URL,
            params={"symbol": symbol, "interval": TIMEFRAME, "limit": limit}
        ) as r:
            d = await r.json()
            return d if isinstance(d, list) else []

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
    e7 = ema(c, 7)
    e25 = ema(c, 25)
    a = atr(h, l, c)

    if not a:
        LAST_REJECT_REASON[pair] = "ATR_FAIL"
        return None

    if price > e7 > e25:
        side = "LONG"
    elif price < e7 < e25:
        side = "SHORT"
    else:
        LAST_REJECT_REASON[pair] = "TREND_FLAT"
        return None

    return {"price": price, "side": side, "atr": a}

# ================== GRID ==================
def build_grid(price, atr_val, side):
    levels = 8
    rng = atr_val * 2.5

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

        STATS["orders_total"] += 1

        exp = abs(exit - entry) * qty
        fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)

        if entry * qty < MIN_ORDER_NOTIONAL or exp - fees < MIN_EXPECTED_PNL:
            STATS["orders_filtered"] += 1
            continue

        orders.append({"entry": entry, "exit": exit, "qty": qty, "open": False})

    if len(orders) < 3:
        STATS["grids_rejected"] += 1
        LAST_REJECT_REASON["grid"] = "FILTERED"
        return None

    STATS["grids_started"] += 1
    return {
        "side": side,
        "low": min(low, high),
        "high": max(low, high),
        "orders": orders,
        "atr": atr_val
    }

def calc_pnl(entry, exit, qty, side):
    gross = (exit - entry) * qty if side == "LONG" else (entry - exit) * qty
    fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)
    return gross - fees

# ================== ENGINE ==================
async def grid_engine():
    while True:
        for pair, g in list(ACTIVE_GRIDS.items()):
            kl = await get_klines(pair, limit=2)
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

                        STATS["total_pnl"] += pnl
                        STATS["deals"] += 1

                        if pnl > 0:
                            STATS["wins"] += 1
                            STATS["gross_profit"] += pnl
                        else:
                            STATS["losses"] += 1
                            STATS["gross_loss"] += pnl

                        STATS["equity"] = DEPOSIT + STATS["total_pnl"]
                        STATS["max_equity"] = max(STATS["max_equity"], STATS["equity"])
                        STATS["max_dd"] = min(
                            STATS["max_dd"],
                            (STATS["equity"] - STATS["max_equity"]) / STATS["max_equity"] * 100
                        )

                        PAIR_STATS.setdefault(pair, {"pnl": 0.0, "deals": 0})
                        PAIR_STATS[pair]["pnl"] += pnl
                        PAIR_STATS[pair]["deals"] += 1

                        o["open"] = False
                        save_state()

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
@dp.message(Command("stats"))
async def stats(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    pf = abs(STATS["gross_profit"] / STATS["gross_loss"]) if STATS["gross_loss"] else float("inf")
    avg = STATS["total_pnl"] / STATS["deals"] if STATS["deals"] else 0
    wr = (STATS["wins"] / STATS["deals"] * 100) if STATS["deals"] else 0

    lines = [
        "📊 GRID BOT — FULL STATS",
        "",
        f"Equity: {STATS['equity']:.2f}$ | ROI: {(STATS['equity']/DEPOSIT-1)*100:.2f}%",
        f"Deals: {STATS['deals']} | Avg PnL: {avg:.4f}$ | PF: {pf:.2f}",
        f"Win rate: {wr:.1f}%",
        "",
        f"Grids active: {len(ACTIVE_GRIDS)}/{MAX_GRIDS}",
        f"Grids started: {STATS['grids_started']}",
        f"Grids rejected: {STATS['grids_rejected']}",
        "",
        f"Orders total: {STATS['orders_total']}",
        f"Orders filtered: {STATS['orders_filtered']}",
        "",
        "Pairs:"
    ]

    for p in ACTIVE_PAIRS:
        status = "ACTIVE" if p in ACTIVE_GRIDS else "NO GRID"
        reason = LAST_REJECT_REASON.get(p, "")
        lines.append(f"• {p}: {status} {('| ' + reason) if reason else ''}")

    await msg.answer("\n".join(lines))

@dp.message(Command("pairs"))
async def pairs(msg: types.Message):
    if msg.from_user.id == ADMIN_ID:
        await msg.answer("Active pairs:\n" + "\n".join(ACTIVE_PAIRS))

@dp.message(Command("pair"))
async def pair_cmd(msg: types.Message):
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
            await msg.answer(f"{pair} added")
    elif action == "remove":
        if pair in ACTIVE_PAIRS:
            ACTIVE_PAIRS.remove(pair)
            ACTIVE_GRIDS.pop(pair, None)
            save_state()
            await msg.answer(f"{pair} removed")

@dp.message(Command("why"))
async def why(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    lines = ["🤔 WHY NO GRID", ""]
    for p in ACTIVE_PAIRS:
        lines.append(f"{p}: {LAST_REJECT_REASON.get(p, 'waiting')}")
    await msg.answer("\n".join(lines))

# ================== MAIN ==================
async def main():
    load_state()
    asyncio.create_task(grid_engine())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())