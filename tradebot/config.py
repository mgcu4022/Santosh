"""Load a YAML config and build the desk (agents, broker, data)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import yaml

from .agents.analyst import AIAnalyst, AnalystConfig
from .agents.desk import Book, TradingDesk
from .brokers.paper import PaperBroker
from .costs import ChargeModel
from .data.csv_feed import load_csv
from .data.synthetic import synthetic_intraday
from .models import Instrument, Product, Segment
from .risk import RiskConfig, RiskManager
from .strategies import build_strategy


@dataclass
class InstrumentSpec:
    instrument: Instrument
    strategy_cfg: dict
    source: dict
    data_key: str


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def parse_instruments(cfg: dict) -> list[InstrumentSpec]:
    specs = []
    for raw in cfg["instruments"]:
        raw = dict(raw)
        strat = raw.pop("strategy")
        source = raw.pop("source", {"type": "synthetic"})
        data_key = raw.pop("data_key", "") or raw.get("instrument_key", "")
        raw["segment"] = Segment(raw.get("segment", "EQ"))
        raw["product"] = Product(raw.get("product", "INTRADAY"))
        specs.append(InstrumentSpec(Instrument(**raw), strat, source, data_key))
    return specs


def build_desk(cfg: dict, live_clock: bool = False, analyst_client=None) -> tuple[TradingDesk, list[InstrumentSpec]]:
    specs = parse_instruments(cfg)
    risk_cfg = RiskConfig.from_dict({"capital": cfg.get("capital", 500_000), **cfg.get("risk", {})})
    risk = RiskManager(risk_cfg)
    broker = PaperBroker(risk_cfg.capital, ChargeModel(cfg.get("charges", {}) or {}),
                         slippage_bps=cfg.get("slippage_bps", 2.0), margin_fn=risk.margin_fraction)
    books = []
    for s in specs:
        params = dict(s.strategy_cfg)
        name = params.pop("name")
        # Shorting equity delivery or buying-only option books: disable shorts at the strategy level too.
        if s.instrument.segment is Segment.OPT or s.instrument.product is Product.DELIVERY:
            params.setdefault("allow_short", False)
        books.append(Book(s.instrument, build_strategy(name, **params)))
    analyst_cfg = AnalystConfig.from_dict(cfg.get("analyst"))
    analyst = AIAnalyst(analyst_cfg, client=analyst_client) if analyst_cfg.enabled else None
    return TradingDesk(books, risk, broker, analyst, live_clock=live_clock), specs


def load_history(cfg: dict, specs: list[InstrumentSpec]) -> dict[str, pd.DataFrame]:
    tf = cfg.get("timeframe", {"unit": "minutes", "interval": 5})
    bt = cfg.get("backtest", {})
    out = {}
    for n, s in enumerate(specs):
        src = s.source
        kind = src.get("type", "synthetic")
        if kind == "csv":
            df = load_csv(src["path"])
        elif kind == "upstox":
            from .data.upstox import UpstoxData
            to_d = date.fromisoformat(str(bt["to"])) if bt.get("to") else date.today()
            from_d = date.fromisoformat(str(bt["from"])) if bt.get("from") else to_d - timedelta(days=60)
            df = UpstoxData().historical(s.data_key, tf["unit"], tf["interval"], from_d, to_d)
        elif kind == "synthetic":
            df = synthetic_intraday(days=src.get("days", 60), start_price=src.get("start_price", 1000.0),
                                    interval_min=tf.get("interval", 5), seed=src.get("seed", 100 + n))
        else:
            raise ValueError(f"Unknown data source type '{kind}' for {s.instrument.symbol}")
        if bt.get("from"):
            df = df[df.index.date >= date.fromisoformat(str(bt["from"]))]
        if bt.get("to"):
            df = df[df.index.date <= date.fromisoformat(str(bt["to"]))]
        if df.empty:
            raise ValueError(f"No data for {s.instrument.symbol}")
        out[s.instrument.symbol] = df
    return out


def env_check() -> dict:
    return {"UPSTOX_ACCESS_TOKEN": bool(os.environ.get("UPSTOX_ACCESS_TOKEN")),
            "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY"))}
