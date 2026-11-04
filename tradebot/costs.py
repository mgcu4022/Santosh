"""Indian brokerage and statutory charges per order.

Defaults are approximate NSE / Upstox rates. Rates change with budgets and
exchange circulars, so check them against your broker's charge sheet and
override them in the config file (`charges:` section).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Product, Segment, Side

# Keys: (segment, product-class) where product-class is "intraday", "delivery" or "fno"
DEFAULT_RATES: dict[str, dict[str, float]] = {
    "eq_delivery": {
        "brokerage_flat": 20.0, "brokerage_pct": 0.0,     # Upstox: Rs 20/order on delivery
        "stt_buy": 0.001, "stt_sell": 0.001,              # 0.1% both sides
        "exchange_txn": 0.0000297,                        # NSE 0.00297%
        "stamp_buy": 0.00015,                             # 0.015% buy side
        "dp_charge_sell": 18.5,                           # depository charge per sell (approx)
    },
    "eq_intraday": {
        "brokerage_flat": 20.0, "brokerage_pct": 0.001,   # lower of Rs 20 or 0.1%
        "stt_buy": 0.0, "stt_sell": 0.00025,              # 0.025% sell side
        "exchange_txn": 0.0000297,
        "stamp_buy": 0.00003,                             # 0.003%
        "dp_charge_sell": 0.0,
    },
    "fut": {
        "brokerage_flat": 20.0, "brokerage_pct": 0.0005,  # lower of Rs 20 or 0.05%
        "stt_buy": 0.0, "stt_sell": 0.0002,               # 0.02% sell side (verify after Budget changes)
        "exchange_txn": 0.0000173,                        # 0.00173%
        "stamp_buy": 0.00002,                             # 0.002%
        "dp_charge_sell": 0.0,
    },
    "opt": {
        "brokerage_flat": 20.0, "brokerage_pct": 0.0,     # Rs 20/order
        "stt_buy": 0.0, "stt_sell": 0.001,                # 0.1% of premium, sell side (verify)
        "exchange_txn": 0.0003503,                        # 0.03503% of premium
        "stamp_buy": 0.00003,                             # 0.003%
        "dp_charge_sell": 0.0,
    },
}
# Broker brokerage plans (only brokerage differs; taxes and exchange fees are the same everywhere).
# Sources (Nov 2026, verify on the broker's rate card): Upstox charges list; Kotak Neo Trade Free plan.
BROKER_PRESETS: dict[str, dict[str, dict[str, float]]] = {
    "upstox": {   # Rs 20 or 0.1% per intraday order (lower); Rs 20 per F&O order; Rs 20 per delivery order
        "eq_intraday": {"brokerage_flat": 20.0, "brokerage_pct": 0.001},
        "eq_delivery": {"brokerage_flat": 20.0, "brokerage_pct": 0.0},
        "fut": {"brokerage_flat": 20.0, "brokerage_pct": 0.0},
        "opt": {"brokerage_flat": 20.0, "brokerage_pct": 0.0},
    },
    "kotak": {    # Kotak Neo Trade Free (Youth/Pro): Rs 10 or 0.05% per intraday order (lower); Rs 10 per F&O order
        "eq_intraday": {"brokerage_flat": 10.0, "brokerage_pct": 0.0005},
        "fut": {"brokerage_flat": 10.0, "brokerage_pct": 0.0},
        "opt": {"brokerage_flat": 10.0, "brokerage_pct": 0.0},
    },
}

SEBI_FEE = 0.000001   # Rs 10 per crore
GST = 0.18            # on brokerage + exchange + SEBI fee


def rate_key(segment: Segment, product: Product) -> str:
    if segment is Segment.FUT:
        return "fut"
    if segment is Segment.OPT:
        return "opt"
    return "eq_delivery" if product is Product.DELIVERY else "eq_intraday"


@dataclass
class ChargeModel:
    overrides: dict[str, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def for_broker(cls, broker: str | None, overrides: dict | None = None) -> "ChargeModel":
        """Preset for a broker ("kotak", "upstox"), with optional config overrides on top."""
        merged: dict[str, dict[str, float]] = {}
        if broker:
            if broker.lower() not in BROKER_PRESETS:
                raise ValueError(f"Unknown broker '{broker}'. Known: {', '.join(BROKER_PRESETS)}")
            for k, v in BROKER_PRESETS[broker.lower()].items():
                merged[k] = dict(v)
        for k, v in (overrides or {}).items():
            merged.setdefault(k, {}).update(v)
        return cls(merged)

    def rates(self, key: str) -> dict[str, float]:
        r = dict(DEFAULT_RATES[key])
        r.update(self.overrides.get(key, {}))
        return r

    def order_charges(self, segment: Segment, product: Product, side: Side,
                      price: float, qty: int) -> float:
        """Total charges in rupees for one executed order."""
        r = self.rates(rate_key(segment, product))
        turnover = abs(price * qty)
        if r["brokerage_pct"] > 0:
            brokerage = min(r["brokerage_flat"], turnover * r["brokerage_pct"])
        else:
            brokerage = r["brokerage_flat"]
        stt = turnover * (r["stt_buy"] if side is Side.BUY else r["stt_sell"])
        exch = turnover * r["exchange_txn"]
        sebi = turnover * SEBI_FEE
        stamp = turnover * r["stamp_buy"] if side is Side.BUY else 0.0
        gst = GST * (brokerage + exch + sebi)
        dp = r["dp_charge_sell"] if side is Side.SELL else 0.0
        return round(brokerage + stt + exch + sebi + stamp + gst + dp, 2)
