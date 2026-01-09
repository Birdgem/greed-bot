import os
import json
import asyncio
import aiohttp
import time
from statistics import mean
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# ================== ENV ==================
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

STATE_FILE = "state.json"

# ================== SETTINGS ==================
AUTO_MODE = True
MAX_AUTO_PAIRS = 4

ALL_PAIRS = [
    "SOLUSDT", "BNBUSDT",
    "DOGEUSDT", "TRXUSDT",
    "ADAUSDT", "XRPUSDT",
    "TONUSDT", "ARBUSDT",
    "OPUSDT"
]

TIMEFRAME = "15m"
BINANCE_URL = "https://api.binance.com/api/v3/klines"

DEPOSIT = 100.0            # ОБЩИЙ депозит
LEVERAGE = 10
MAX_GRIDS = 2
MAX_MARGIN_PER_GRID = 0.10 # доля депо на 1 сетку

MAKER_FEE = 0.0002
TAKER_FEE = 0.0004

ATR_PERIOD = 14
SCAN_INTERVAL = 20
HEARTBEAT_INTERVAL = 1800

MIN_ORDER_NOTIONAL = 5.0

# ===== RISK =====
STOP_DD_ON  = -20.0   # %
STOP_DD_OFF = -10.0   # %

# ================== STATE ==================
START_TS = time.time()

ACTIVE_PAIRS = ["SOLUSDT", "BNBUSDT"]
AUTO_SELECTED_PAIRS = []
ACTIVE_GRIDS = {}
LAST_REJECT_REASON = {}

TOTAL_PNL = 0.0
DEALS = 0

PAIR_STATS = {}

MAX_EQUITY = DEPOSIT
GLOBAL_STOP = False

# ================== STATE IO ==================
def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "ACTIVE_PAIRS": ACTIVE_PAIRS,
            "AUTO_SELECTED_PAIRS": AUTO_SELECTED_PAIRS,
            "TOTAL_PNL": TOTAL_PNL,
            "DEALS": DEALS,
            "PAIR_STATS": PAIR_STATS,
            "MAX_EQUITY": MAX_EQUITY,
            "GLOBAL_STOP": GLOBAL_STOP
        }, f)

def load_state():
    global ACTIVE_PAIRS, AUTO_SELECTED_PAIRS, TOTAL_PNL, DEALS
    global PAIR_STATS, MAX_EQUITY, GLOBAL_STOP

    if not os.path.exists(STATE_FILE):
        return

    with open(STATE_FILE) as f:
        d = json.load(f)

    ACTIVE_PAIRS = d.get("ACTIVE_PAIRS", ACTIVE_PAIRS)
    AUTO_SELECTED_PAIRS = d.get("AUTO_SELECTED_PAIRS", [])
    TOTAL_PNL = d.get("TOTAL_PNL", 0.0)
    DEALS = d.get("DEALS", 0)
    PAIR_STATS = d.get("PAIR_STATS", {})
    MAX_EQUITY = d.get("MAX_EQUITY", DEPOSIT)
    GLOBAL_STOP = d.get("GLOBAL_STOP", False)

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

# ================== AUTO PAIRS ==================
async def auto_select_pairs():
    global AUTO_SELECTED_PAIRS
    scored = []

    for pair in ALL_PAIRS:
        kl = await get_klines(pair)
        if len(kl) < 50:
            continue

        c = [float(k[4]) for k in kl]
        h = [float(k[2]) for k in kl]
        l = [float(k[3]) for k in kl]

        price = c[-1]
        a = atr(h, l, c)
        if not a:
            continue

        atr_pct = a / price * 100

        if price > 15:
            continue
        if atr_pct < 0.4 or atr_pct > 3.0:
            continue

        scored.append((pair, abs(atr_pct - 1.2)))

    scored.sort(key=lambda x: x[1])
    AUTO_SELECTED_PAIRS = [p for p, _ in scored[:MAX_AUTO_PAIRS]]

# ================== ANALYSIS ==================
async def analyze_pair(pair):
    kl = await get_klines(pair)
    if len(kl) < 50:
        return None

    c = [float(k[4]) for k in kl]
    h = [float(k[2]) for k in kl]
    l = [float(k[3]) for k in kl]

    price = c[-1]
    e7 = ema(c, 7)
    e25 = ema(c, 25)
    a = atr(h, l, c)

    if price > e7 > e25:
        side = "LONG"
    elif price < e7 < e25:
        side = "SHORT"
    else:
        side = "FLAT"

    return {"price": price, "side": side, "atr": a}

# ================== GRID ==================
def build_grid(price, atr_val):
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
        entry = low + step * i
        exit = entry + step
        if entry * qty < MIN_ORDER_NOTIONAL:
            continue
        orders.append({"entry": entry, "exit": exit, "qty": qty, "open": False})

    return {"low": low, "high": high, "orders": orders, "atr": atr_val}

def calc_pnl(entry, exit, qty):
    gross = (exit - entry) * qty
    fees = (entry * qty * MAKER_FEE) + (exit * qty * TAKER_FEE)
    return gross - fees

# ================== ENGINE ==================
async def grid_engine():
    global TOTAL_PNL, DEALS, MAX_EQUITY, GLOBAL_STOP

    while True:
        if AUTO_MODE:
            await auto_select_pairs()

        all_pairs = list(set(ACTIVE_PAIRS + AUTO_SELECTED_PAIRS))
        equity = DEPOSIT + TOTAL_PNL

        # ---- max equity / drawdown ----
        if equity > MAX_EQUITY:
            MAX_EQUITY = equity

        dd = (equity - MAX_EQUITY) / MAX_EQUITY * 100

        if not GLOBAL_STOP and dd <= STOP_DD_ON:
            GLOBAL_STOP = True

        if GLOBAL_STOP and dd >= STOP_DD_OFF:
            GLOBAL_STOP = False

        # ---- update grids ----
        for pair, g in list(ACTIVE_GRIDS.items()):
            kl = await get_klines(pair, 2)
            if not kl:
                continue

            price = float(kl[-1][4])

            if pair not in all_pairs or not (g["low"] <= price <= g["high"]):
                del ACTIVE_GRIDS[pair]
                continue

            for o in g["orders"]:
                if not o["open"] and price <= o["entry"]:
                    o["open"] = True
                elif o["open"] and price >= o["exit"]:
                    pnl = calc_pnl(o["entry"], o["exit"], o["qty"])
                    TOTAL_PNL += pnl
                    DEALS += 1

                    PAIR_STATS.setdefault(pair, {"pnl": 0.0, "deals": 0})
                    PAIR_STATS[pair]["pnl"] += pnl
                    PAIR_STATS[pair]["deals"] += 1

                    o["open"] = False
                    save_state()

        # ---- start new grids ----
        if not GLOBAL_STOP and len(ACTIVE_GRIDS) < MAX_GRIDS:
            for pair in all_pairs:
                if pair in ACTIVE_GRIDS:
                    continue
                res = await analyze_pair(pair)
                if not res:
                    continue
                ACTIVE_GRIDS[pair] = build_grid(res["price"], res["atr"])
                if len(ACTIVE_GRIDS) >= MAX_GRIDS:
                    break

        await asyncio.sleep(SCAN_INTERVAL)

# ================== COMMANDS ==================
@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    equity = DEPOSIT + TOTAL_PNL
    roi = (equity - DEPOSIT) / DEPOSIT * 100
    uptime = int((time.time() - START_TS) / 60)
    dd = (equity - MAX_EQUITY) / MAX_EQUITY * 100

    lines = [
        "📊 GRID BOT STATUS",
        f"Uptime: {uptime} min",
        f"Equity: {equity:.2f}$ | ROI: {roi:.2f}%",
        f"Max equity: {MAX_EQUITY:.2f}$",
        f"Drawdown: {dd:.2f}%",
        f"GLOBAL STOP: {'ON 🔴' if GLOBAL_STOP else 'OFF 🟢'}",
        f"Deals: {DEALS}",
        "",
        "Pairs:"
    ]

    for p in set(ACTIVE_PAIRS + AUTO_SELECTED_PAIRS):
        ps = PAIR_STATS.get(p)
        status = "GRID" if p in ACTIVE_GRIDS else "WAIT"
        if not ps:
            lines.append(f"• {p} | {status} | no trades")
        else:
            avg = ps["pnl"] / ps["deals"]
            lines.append(
                f"• {p} | {status}\n"
                f"  Deals: {ps['deals']} | PnL: {ps['pnl']:.2f}$ | Avg: {avg:.3f}$"
            )

    await msg.answer("\n".join(lines))

@dp.message(Command("resume"))
async def cmd_resume(msg: types.Message):
    global GLOBAL_STOP
    GLOBAL_STOP = False
    await msg.answer("🟢 GLOBAL STOP снят вручную")

# ================== MAIN ==================
async def main():
    load_state()
    asyncio.create_task(grid_engine())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())