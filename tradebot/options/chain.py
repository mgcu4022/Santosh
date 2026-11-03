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
    close: float = 0.0          # previous day's close of this option (for % change)

    @property
    def chg_pct(self) -> float | None:
        return round((self.ltp / self.close - 1) * 100, 1) if self.close and self.ltp else None


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

    def analytics(self) -> dict:
        """Plain market read-outs from the whole chain (OI = open contracts)."""
        ks = self.strikes
        if not ks:
            return {}
        ce_oi = {k: self.rows[k]["CE"].oi for k in ks}
        pe_oi = {k: self.rows[k]["PE"].oi for k in ks}
        tot_ce, tot_pe = sum(ce_oi.values()), sum(pe_oi.values())

        def pain(x):   # total value option buyers would get at expiry if NIFTY ends at x
            return sum(ce_oi[k] * max(x - k, 0) + pe_oi[k] * max(k - x, 0) for k in ks)
        atm = self.atm()
        ivs = [self.rows[atm][o].iv for o in ("CE", "PE") if self.rows[atm][o].iv]
        return {
            "pcr": round(tot_pe / tot_ce, 2) if tot_ce else None,
            "resistance": max(ks, key=lambda k: ce_oi[k]) if tot_ce else None,   # most call writing
            "support": max(ks, key=lambda k: pe_oi[k]) if tot_pe else None,      # most put writing
            "max_pain": min(ks, key=pain) if (tot_ce or tot_pe) else None,
            "atm_iv": round(sum(ivs) / len(ivs), 1) if ivs else None,
        }

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
            row[name] = Leg(o.get("instrument_key", ""), float(md.get("ltp") or 0), oi, oi - prev,
                            float(gk.get("iv") or 0), float(md.get("close_price") or 0))
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
    """Simulated index and option prices (Black-Scholes) for demo mode. Not real data.

    The index moves about 1% per trading day (scaled to the simulated clock), open
    interest has a realistic shape (puts below spot, calls above) and options carry
    a previous close so % changes make sense.
    """
    DAILY_VOL = 0.011
    SESSION_SECONDS = 22_500           # 09:15-15:30

    def __init__(self, seed: int = 7, clock=None):
        self.rng = random.Random(seed)
        self.seed = seed
        self.clock = clock or datetime.now
        self.spot = {u: v[3] for u, v in UNDERLYINGS.items()}
        self.open = dict(self.spot)
        self.hi, self.lo = dict(self.spot), dict(self.spot)
        self.last_t: dict[str, datetime] = {}
        self.closes: dict[tuple, float] = {}

    def expiries(self, underlying: str) -> list[str]:
        d = self.clock().date()
        tuesdays = [d + timedelta(days=(1 - d.weekday()) % 7 + 7 * w) for w in range(4)]
        return [x.isoformat() for x in tuesdays]

    def _advance(self, underlying: str, now: datetime) -> float:
        last = self.last_t.get(underlying)
        self.last_t[underlying] = now
        if last is not None and now > last:
            dt = (now - last).total_seconds()
            sigma = self.DAILY_VOL * math.sqrt(dt / self.SESSION_SECONDS)
            self.spot[underlying] *= math.exp(self.rng.gauss(0, sigma))
        s = self.spot[underlying]
        self.hi[underlying], self.lo[underlying] = max(self.hi[underlying], s), min(self.lo[underlying], s)
        return s

    @staticmethod
    def _iv(k: float, s: float) -> float:
        m = math.log(k / s)
        return 0.115 + 1.5 * abs(m) + (0.6 * abs(m) if k < s else 0)   # smile, puts richer

    def _oi(self, underlying, expiry, k, opt, s0, step) -> tuple[float, float]:
        r = random.Random(f"{self.seed}|{underlying}|{expiry}|{k}|{opt}")
        peak = s0 + (4 if opt == "CE" else -4) * step     # call writers above, put writers below
        dist = (k - peak) / (step * 6)
        centre = 1.0 if (opt == "PE" and k <= s0) or (opt == "CE" and k >= s0) else 0.35
        base = centre * math.exp(-dist * dist / 2) * r.uniform(0.4, 1.6)
        if k % (step * 10) == 0:
            base *= 1.8                                # round strikes attract more OI
        lots = max(base * 120_000, 2_000)
        return round(lots, -3), round(lots * r.uniform(-0.25, 0.35), -3)

    def chain(self, underlying: str, expiry: str) -> Chain:
        _, step, lot, _ = UNDERLYINGS[underlying]
        now = self.clock()
        s = self._advance(underlying, now)
        s0 = self.open[underlying]
        exp_dt = datetime.combine(date.fromisoformat(expiry), datetime.min.time()).replace(hour=15, minute=30)
        if now.tzinfo:
            exp_dt = exp_dt.replace(tzinfo=now.tzinfo)
        t = max((exp_dt - now).total_seconds(), 3600) / (365 * 86400)
        t_prev = t + 1 / 365
        atm = round(s / step) * step
        rows = {}
        for i in range(-20, 21):
            k = float(atm + i * step)
            iv = self._iv(k, s)
            row = {}
            for o in ("CE", "PE"):
                key = (underlying, expiry, k, o)
                if key not in self.closes:     # yesterday's close, priced at the opening spot
                    self.closes[key] = round(max(bs_price(s0, k, t_prev, self._iv(k, s0), o), 0.05), 2)
                oi, chg = self._oi(underlying, expiry, k, o, s0, step)
                row[o] = Leg(f"SIM|{underlying}|{expiry}|{k:g}|{o}", round(max(bs_price(s, k, t, iv, o), 0.05), 2),
                             oi=oi, oi_chg=chg, iv=round(iv * 100, 1), close=self.closes[key])
            rows[k] = row
        return Chain(underlying, expiry, round(s, 2), rows, prev_close=s0,
                     day_high=round(self.hi[underlying], 2), day_low=round(self.lo[underlying], 2),
                     lot_size=lot, step=step)
