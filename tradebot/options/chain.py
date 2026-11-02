"""Option chain data: Upstox (live) and a simulator (demo mode, no login needed)."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import quote

UNDERLYINGS = {
    # name: (Upstox key, strike step, default lot size, approx. spot for demo). Lot sizes change: verify on NSE.
    "NIFTY": ("NSE_INDEX|Nifty 50", 50, 65, 23200.0),
    "BANKNIFTY": ("NSE_INDEX|Nifty Bank", 100, 30, 52000.0),
    "FINNIFTY": ("NSE_INDEX|Nifty Fin Service", 50, 60, 24500.0),
    "SENSEX": ("BSE_INDEX|SENSEX", 100, 20, 76000.0),
}


@dataclass
class Leg:
    key: str = ""
    ltp: float = 0.0
    oi: float = 0.0
    oi_chg: float = 0.0
    iv: float = 0.0


@dataclass
class Chain:
    underlying: str
    expiry: str
    spot: float
    rows: dict[float, dict[str, Leg]] = field(default_factory=dict)   # strike -> {"CE": Leg, "PE": Leg}
    prev_close: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    lot_size: int = 1
    step: float = 50

    @property
    def strikes(self) -> list[float]:
        return sorted(self.rows)

    def atm(self) -> float:
        return min(self.rows, key=lambda k: abs(k - self.spot)) if self.rows else round(self.spot / self.step) * self.step

    def ltp(self, strike: float, opt: str) -> float | None:
        leg = self.rows.get(strike, {}).get(opt)
        return leg.ltp if leg and leg.ltp > 0 else None

    def straddle(self) -> float | None:
        k = self.atm()
        ce, pe = self.ltp(k, "CE"), self.ltp(k, "PE")
        return round(ce + pe, 2) if ce and pe else None

    def synthetic_future(self) -> float | None:
        """Put-call parity: F = K + CE - PE at the ATM strike."""
        k = self.atm()
        ce, pe = self.ltp(k, "CE"), self.ltp(k, "PE")
        return round(k + ce - pe, 2) if ce and pe else None

    def window(self, n: int) -> list[float]:
        """n strikes either side of ATM."""
        ks = self.strikes
        if not ks:
            return []
        i = ks.index(self.atm())
        return ks[max(0, i - n): i + n + 1]


# --- Upstox -----------------------------------------------------------------------------
def parse_upstox_chain(data: list[dict], underlying: str, expiry: str) -> Chain:
    spot = 0.0
    rows: dict[float, dict[str, Leg]] = {}
    for it in data or []:
        k = float(it["strike_price"])
        spot = float(it.get("underlying_spot_price") or spot)
        row = {}
        for side, name in (("call_options", "CE"), ("put_options", "PE")):
            o = it.get(side) or {}
            md, gk = o.get("market_data") or {}, o.get("option_greeks") or {}
            oi, prev = float(md.get("oi") or 0), float(md.get("prev_oi") or 0)
            row[name] = Leg(o.get("instrument_key", ""), float(md.get("ltp") or 0), oi, oi - prev, float(gk.get("iv") or 0))
        rows[k] = row
    name = underlying
    return Chain(name, expiry, spot, rows, step=UNDERLYINGS.get(name, ("", 50))[1],
                 lot_size=UNDERLYINGS.get(name, ("", 50, 1))[2])


class UpstoxOptions:
    """Live chain from Upstox (needs today's login token)."""

    def __init__(self, upstox):
        self.u = upstox            # tradebot.data.upstox.UpstoxData
        self._lots: dict[str, int] = {}

    def expiries(self, underlying: str) -> list[str]:
        key = UNDERLYINGS[underlying][0]
        data = self.u._get("/v2/option/contract", params={"instrument_key": key})
        exps = sorted({c["expiry"] for c in data if c.get("expiry")})
        for c in data:
            if c.get("lot_size"):
                self._lots[underlying] = int(c["lot_size"])   # real lot size from the exchange
                break
        return [e for e in exps if e >= date.today().isoformat()]

    def chain(self, underlying: str, expiry: str) -> Chain:
        key = UNDERLYINGS[underlying][0]
        ch = parse_upstox_chain(self.u._get("/v2/option/chain", params={"instrument_key": key, "expiry_date": expiry}),
                                underlying, expiry)
        ch.lot_size = self._lots.get(underlying, ch.lot_size)
        try:
            q = self.u._get("/v2/market-quote/quotes", params={"instrument_key": key})
            v = next(iter(q.values()))
            ohlc = v.get("ohlc") or {}
            ch.day_high, ch.day_low = ohlc.get("high"), ohlc.get("low")
            if v.get("net_change") is not None:
                ch.prev_close = float(v["last_price"]) - float(v["net_change"])
        except Exception:
            pass
        return ch


# --- Simulator (demo) ----------------------------------------------------------------------
def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot: float, strike: float, t_years: float, iv: float, opt: str, r: float = 0.065) -> float:
    t = max(t_years, 1e-6)
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if opt == "CE":
        return spot * _ncdf(d1) - strike * math.exp(-r * t) * _ncdf(d2)
    return strike * math.exp(-r * t) * _ncdf(-d2) - spot * _ncdf(-d1)


class SimOptions:
    """Simulated index and option prices (Black-Scholes) for demo mode. Not real data."""

    def __init__(self, seed: int = 7, clock=None):
        self.rng = random.Random(seed)
        self.clock = clock or datetime.now
        self.spot = {u: v[3] for u, v in UNDERLYINGS.items()}
        self.open = dict(self.spot)
        self.hi, self.lo = dict(self.spot), dict(self.spot)

    def expiries(self, underlying: str) -> list[str]:
        d = self.clock().date()
        tuesdays = [d + timedelta(days=(1 - d.weekday()) % 7 + 7 * w) for w in range(4)]
        return [x.isoformat() for x in tuesdays]

    def chain(self, underlying: str, expiry: str) -> Chain:
        _, step, lot, _ = UNDERLYINGS[underlying]
        s = self.spot[underlying] * math.exp(self.rng.gauss(0, 0.0006))
        self.spot[underlying] = s
        self.hi[underlying], self.lo[underlying] = max(self.hi[underlying], s), min(self.lo[underlying], s)
        now = self.clock()
        exp_dt = datetime.combine(date.fromisoformat(expiry), datetime.min.time()).replace(hour=15, minute=30)
        if now.tzinfo:
            exp_dt = exp_dt.replace(tzinfo=now.tzinfo)
        t = max((exp_dt - now).total_seconds(), 3600) / (365 * 86400)
        atm = round(s / step) * step
        rows = {}
        for i in range(-20, 21):
            k = atm + i * step
            iv = 0.12 + 0.6 * (math.log(k / s)) ** 2 + (0.02 if k < s else 0)   # mild smile/skew
            rows[float(k)] = {o: Leg(f"SIM|{underlying}|{expiry}|{k}|{o}", round(max(bs_price(s, k, t, iv, o), 0.05), 2),
                                     oi=float(self.rng.randint(5, 150) * 1000 * max(1, 6 - abs(i) // 3)),
                                     oi_chg=float(self.rng.randint(-20, 20) * 1000), iv=round(iv * 100, 1))
                    for o in ("CE", "PE")}
        return Chain(underlying, expiry, round(s, 2), rows, prev_close=self.open[underlying],
                     day_high=round(self.hi[underlying], 2), day_low=round(self.lo[underlying], 2),
                     lot_size=lot, step=step)
