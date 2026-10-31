"""Load a YAML config and build the desk (agents, broker, data)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, time, timedelta

import pandas as pd
import yaml

from .agents.analyst import AIAnalyst, AnalystConfig
from .agents.desk import Book, TradingDesk
from .brokers.paper import PaperBroker
from .costs import ChargeModel
from .data.csv_feed import load_csv
from .data.synthetic import synthetic_daily, synthetic_intraday
from .models import Instrument, Product, Segment
from .risk import RiskConfig, RiskManager
from .strategies import build_strategy


@dataclass
class InstrumentSpec:
    instrument: Instrument
    strategy_cfg: dict
    source: dict
    data_key: str
    backup: dict = None          # e.g. {kotak_token: "2885", kotak_segment: nse_cm}


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
        backup = raw.pop("backup", None) or {}
        raw["segment"] = Segment(raw.get("segment", "EQ"))
        raw["product"] = Product(raw.get("product", "INTRADAY"))
        specs.append(InstrumentSpec(Instrument(**raw), strat, source, data_key, backup))
    return specs


def is_daily(cfg: dict) -> bool:
    return cfg.get("timeframe", {}).get("unit", "minutes") in ("days", "weeks", "months")


def build_desk(cfg: dict, live_clock: bool = False, analyst_client=None,
               notifier=None) -> tuple[TradingDesk, list[InstrumentSpec]]:
    specs = parse_instruments(cfg)
    risk_d = {"capital": cfg.get("capital", 500_000), **cfg.get("risk", {})}
    if is_daily(cfg):
        # Daily bars carry no intraday clock: drop the entry window and same-day square-off.
        risk_d.update(no_entry_before="00:00", no_entry_after="23:59", square_off_time="23:59:59")
        for s in specs:
            if s.instrument.product is Product.INTRADAY:
                raise ValueError(f"{s.instrument.symbol}: use product DELIVERY (equity) or NRML (F&O) with daily bars")
    risk_cfg = RiskConfig.from_dict(risk_d)
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
    return TradingDesk(books, risk, broker, analyst, live_clock=live_clock, notifier=notifier), specs


def load_history(cfg: dict, specs: list[InstrumentSpec]) -> dict[str, pd.DataFrame]:
    tf = cfg.get("timeframe", {"unit": "minutes", "interval": 5})
    bt = cfg.get("backtest", {})
    out = {}
    google_sheets: dict = {}   # one download per sheet URL
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
        elif kind == "yahoo":
            from .data.yahoo import YahooData, to_yahoo_ticker
            ticker = src.get("ticker") or to_yahoo_ticker(s.instrument.symbol, s.instrument.exchange)
            interval = "1d" if is_daily(cfg) else f"{tf.get('interval', 5)}m"
            to_d = date.fromisoformat(str(bt["to"])) if bt.get("to") else date.today()
            default_days = 365 * 3 if is_daily(cfg) else 59      # Yahoo keeps ~60 days of 5m data
            from_d = date.fromisoformat(str(bt["from"])) if bt.get("from") else to_d - timedelta(days=default_days)
            df = YahooData().history(ticker, interval, from_d, to_d)
            if not is_daily(cfg):
                t = df.index.time
                df = df[(t >= time(9, 15)) & (t < time(15, 30))]
        elif kind == "google":
            if not is_daily(cfg):
                raise ValueError(f"{s.instrument.symbol}: Google Finance only has daily candles; "
                                 "set timeframe: {unit: days, interval: 1}")
            from .data.google import GoogleSheetData
            url = src.get("url") or cfg.get("google_sheet_url")
            if not url:
                raise ValueError(f"{s.instrument.symbol}: google source needs url (or top-level google_sheet_url)")
            sheet = google_sheets.setdefault(url, GoogleSheetData(url, dayfirst=src.get("dayfirst")))
            df = sheet.history(src.get("symbol", s.instrument.symbol))
        elif kind == "synthetic":
            if is_daily(cfg):
                df = synthetic_daily(days=src.get("days", 500), start_price=src.get("start_price", 1000.0),
                                     seed=src.get("seed", 100 + n))
            else:
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
            "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "TELEGRAM_BOT_TOKEN": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
            "TELEGRAM_CHAT_ID": bool(os.environ.get("TELEGRAM_CHAT_ID")),
            "KOTAK_CONSUMER_KEY": bool(os.environ.get("KOTAK_CONSUMER_KEY")),
            "KOTAK_MOBILE/UCC/MPIN": all(os.environ.get(k) for k in ("KOTAK_MOBILE", "KOTAK_UCC", "KOTAK_MPIN"))}
